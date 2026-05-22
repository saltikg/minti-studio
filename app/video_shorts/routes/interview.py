import tempfile
import re
import os
import subprocess
from datetime import datetime
from pathlib import Path
from uuid import uuid4
from zoneinfo import ZoneInfo

from flask import Response, flash, g, jsonify, redirect, render_template, request, url_for

from app.video_shorts import video_shorts_bp
from app.video_shorts.config import WHISPER_MODEL, _openai_client
from app.video_shorts.services.db import ensure_interview_practice_schema, get_db, get_db_readonly

PACIFIC_TZ = ZoneInfo("America/Los_Angeles")


def _require_user_id():
    current_user = getattr(g, "vs_current_user", None)
    if not current_user:
        return None
    return str(current_user.get("id") or "").strip() or None


def _split_tags(raw: str) -> list[str]:
    tags = []
    seen = set()
    for part in (raw or "").split(","):
        value = part.strip()
        if not value:
            continue
        key = value.lower()
        if key in seen:
            continue
        seen.add(key)
        tags.append(value)
    return tags


def _parse_qa_pairs(raw_text: str) -> list[dict]:
    """
    Parse plain text blocks in this format:
      Q: question
      A: answer
    Also accepts Turkish aliases: Soru:, Cevap:
    """
    lines = [str(line or "").rstrip() for line in str(raw_text or "").splitlines()]
    pairs = []
    current_q = None
    answer_lines = []

    def _flush():
        nonlocal current_q, answer_lines, pairs
        q = (current_q or "").strip()
        a = "\n".join(answer_lines).strip()
        if q and a:
            pairs.append({"question": q, "answer": a})
        current_q = None
        answer_lines = []

    for raw_line in lines:
        line = raw_line.strip()
        lower = line.lower()
        is_q = lower.startswith("q:") or lower.startswith("soru:")
        is_a = lower.startswith("a:") or lower.startswith("cevap:")

        if is_q:
            if current_q is not None:
                _flush()
            current_q = line.split(":", 1)[1].strip() if ":" in line else ""
            continue

        if is_a:
            if current_q is None:
                continue
            content = line.split(":", 1)[1].strip() if ":" in line else ""
            answer_lines = [content] if content else []
            continue

        if current_q is not None:
            if answer_lines:
                answer_lines.append(raw_line)
            else:
                current_q = f"{current_q} {line}".strip() if line else current_q

    if current_q is not None:
        _flush()
    return pairs


def _fmt_pacific(dt_obj) -> str:
    if not dt_obj:
        return "-"
    try:
        if isinstance(dt_obj, datetime):
            if dt_obj.tzinfo is None:
                dt_obj = dt_obj.replace(tzinfo=ZoneInfo("UTC"))
            return dt_obj.astimezone(PACIFIC_TZ).strftime("%Y-%m-%d %H:%M %Z")
        parsed = datetime.fromisoformat(str(dt_obj))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=ZoneInfo("UTC"))
        return parsed.astimezone(PACIFIC_TZ).strftime("%Y-%m-%d %H:%M %Z")
    except Exception:
        return str(dt_obj)


def _mime_to_suffix(mime_type: str) -> str:
    mime = str(mime_type or "").lower()
    if "webm" in mime:
        return ".webm"
    if "ogg" in mime:
        return ".ogg"
    if "mp4" in mime or "m4a" in mime:
        return ".m4a"
    if "wav" in mime:
        return ".wav"
    if "mpeg" in mime or "mp3" in mime:
        return ".mp3"
    return ".bin"


def _transcode_audio_to_mp3(audio_bytes: bytes, source_mime: str = "") -> tuple[bytes, str]:
    if not audio_bytes:
        return b"", ""
    src_path = ""
    dst_path = ""
    try:
        with tempfile.NamedTemporaryFile(delete=False, suffix=_mime_to_suffix(source_mime)) as src_file:
            src_file.write(audio_bytes)
            src_path = src_file.name
        with tempfile.NamedTemporaryFile(delete=False, suffix=".mp3") as dst_file:
            dst_path = dst_file.name

        cmd = [
            "ffmpeg",
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-i",
            src_path,
            "-vn",
            "-ac",
            "1",
            "-ar",
            "44100",
            "-b:a",
            "128k",
            dst_path,
        ]
        proc = subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, check=False)
        if proc.returncode != 0:
            return b"", ""
        out_bytes = Path(dst_path).read_bytes()
        if not out_bytes:
            return b"", ""
        return out_bytes, "audio/mpeg"
    except Exception:
        return b"", ""
    finally:
        for p in (src_path, dst_path):
            if p:
                try:
                    os.remove(p)
                except Exception:
                    pass


def _ensure_schema_with_rw():
    conn = get_db()
    try:
        ensure_interview_practice_schema(conn)
    finally:
        conn.close()


def _load_interview_detail(conn, user_id: str, interview_id: str):
    row = conn.execute(
        """
        SELECT id, title, description, created_at
        FROM int_interviews
        WHERE id = ? AND user_id = ?
        """,
        [interview_id, user_id],
    ).fetchone()
    if not row:
        return None

    tag_rows = conn.execute(
        """
        SELECT t.name
        FROM int_interview_tags it
        JOIN int_tags t ON t.id = it.tag_id
        WHERE it.interview_id = ?
        ORDER BY t.name ASC
        """,
        [interview_id],
    ).fetchall()

    return {
        "id": str(row[0]),
        "title": str(row[1] or "").strip(),
        "description": str(row[2] or "").strip(),
        "created_at": row[3],
        "tags": [str(r[0]) for r in tag_rows],
    }


@video_shorts_bp.route("/interview/interviews", methods=["GET", "POST"])
def interview_interviews_page():
    user_id = _require_user_id()
    if not user_id:
        return redirect(url_for("video_shorts_bp.login", next=request.url))

    _ensure_schema_with_rw()

    if request.method == "POST":
        title = str(request.form.get("title") or "").strip()
        tags_raw = str(request.form.get("tags") or "")
        if not title:
            flash("Interview title is required.", "danger")
            return redirect(url_for("video_shorts_bp.interview_interviews_page"))

        interview_id = str(uuid4())
        conn = get_db()
        try:
            ensure_interview_practice_schema(conn)
            conn.execute(
                """
                INSERT INTO int_interviews (id, user_id, title, description, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                [interview_id, user_id, title, "", datetime.utcnow()],
            )

            for tag_name in _split_tags(tags_raw):
                existing = conn.execute(
                    "SELECT id FROM int_tags WHERE user_id = ? AND lower(name) = lower(?)",
                    [user_id, tag_name],
                ).fetchone()
                if existing:
                    tag_id = str(existing[0])
                else:
                    tag_id = str(uuid4())
                    conn.execute(
                        "INSERT INTO int_tags (id, user_id, name, created_at) VALUES (?, ?, ?, ?)",
                        [tag_id, user_id, tag_name, datetime.utcnow()],
                    )
                exists_link = conn.execute(
                    "SELECT 1 FROM int_interview_tags WHERE interview_id = ? AND tag_id = ?",
                    [interview_id, tag_id],
                ).fetchone()
                if not exists_link:
                    conn.execute(
                        "INSERT INTO int_interview_tags (interview_id, tag_id, created_at) VALUES (?, ?, ?)",
                        [interview_id, tag_id, datetime.utcnow()],
                    )

            conn.commit()
        finally:
            conn.close()

        return redirect(url_for("video_shorts_bp.interview_practice_page", interview_id=interview_id))

    conn = get_db_readonly()
    try:
        interviews = []
        rows = conn.execute(
            """
            SELECT id, title, created_at
            FROM int_interviews
            WHERE user_id = ?
            ORDER BY created_at DESC
            """,
            [user_id],
        ).fetchall()
        for row in rows:
            interview_id = str(row[0])
            tag_rows = conn.execute(
                """
                SELECT t.id, t.name
                FROM int_interview_tags it
                JOIN int_tags t ON t.id = it.tag_id
                WHERE it.interview_id = ?
                ORDER BY t.name ASC
                """,
                [interview_id],
            ).fetchall()
            count_row = conn.execute(
                "SELECT COUNT(*) FROM int_recordings WHERE interview_id = ? AND user_id = ?",
                [interview_id, user_id],
            ).fetchone()
            interviews.append(
                {
                    "id": interview_id,
                    "title": str(row[1] or "").strip(),
                    "created_at": row[2],
                    "created_at_pacific": _fmt_pacific(row[2]),
                    "tags": [{"id": str(t[0]), "name": str(t[1])} for t in tag_rows],
                    "recording_count": int((count_row[0] if count_row else 0) or 0),
                }
            )
    finally:
        conn.close()

    return render_template("interview_interviews.html", interviews=interviews)


@video_shorts_bp.route("/interview/practice", methods=["GET"])
def interview_practice_root_redirect():
    user_id = _require_user_id()
    if not user_id:
        return redirect(url_for("video_shorts_bp.login", next=request.url))
    return redirect(url_for("video_shorts_bp.interview_interviews_page"))


@video_shorts_bp.route("/interview/practice/<interview_id>", methods=["GET"])
def interview_practice_page(interview_id: str):
    user_id = _require_user_id()
    if not user_id:
        return redirect(url_for("video_shorts_bp.login", next=request.url))

    _ensure_schema_with_rw()
    conn = get_db_readonly()
    try:
        interview = _load_interview_detail(conn, user_id=user_id, interview_id=interview_id)
    finally:
        conn.close()

    if not interview:
        flash("Interview not found.", "danger")
        return redirect(url_for("video_shorts_bp.interview_interviews_page"))

    return render_template("interview_practice.html", interview=interview)


@video_shorts_bp.route("/api/interview/interviews/<interview_id>/recordings", methods=["GET"])
def interview_recordings_api(interview_id: str):
    user_id = _require_user_id()
    if not user_id:
        return jsonify({"error": "auth required"}), 401

    _ensure_schema_with_rw()
    conn = get_db_readonly()
    try:
        interview = _load_interview_detail(conn, user_id=user_id, interview_id=interview_id)
        if not interview:
            return jsonify({"error": "interview not found"}), 404

        rows = conn.execute(
            """
            SELECT id, note, transcript, secondary_text, mime_type, created_at
            FROM int_recordings
            WHERE interview_id = ? AND user_id = ?
            ORDER BY created_at DESC
            """,
            [interview_id, user_id],
        ).fetchall()
    finally:
        conn.close()

    recordings = []
    for row in rows:
        rec_id = str(row[0])
        recordings.append(
            {
                "id": rec_id,
                "note": str(row[1] or ""),
                "transcript": str(row[2] or ""),
                "secondary_text": str(row[3] or ""),
                "mime_type": str(row[4] or "audio/webm"),
                "created_at": row[5].isoformat() if getattr(row[5], "isoformat", None) else str(row[5] or ""),
                "audio_url": url_for("video_shorts_bp.interview_recording_audio_api", recording_id=rec_id),
            }
        )
    return jsonify({"recordings": recordings})


@video_shorts_bp.route("/api/interview/interviews/<interview_id>/materials", methods=["GET"])
def interview_materials_api(interview_id: str):
    user_id = _require_user_id()
    if not user_id:
        return jsonify({"error": "auth required"}), 401

    _ensure_schema_with_rw()
    conn = get_db_readonly()
    try:
        interview = _load_interview_detail(conn, user_id=user_id, interview_id=interview_id)
        if not interview:
            return jsonify({"error": "interview not found"}), 404

        rows = conn.execute(
            """
            SELECT id, sort_order, question, answer, created_at
            FROM int_interview_materials
            WHERE interview_id = ? AND user_id = ?
            ORDER BY sort_order ASC, created_at ASC
            """,
            [interview_id, user_id],
        ).fetchall()
    finally:
        conn.close()

    materials = []
    for row in rows:
        materials.append(
            {
                "id": str(row[0]),
                "sort_order": int(row[1] or 0),
                "question": str(row[2] or ""),
                "answer": str(row[3] or ""),
                "created_at": row[4].isoformat() if getattr(row[4], "isoformat", None) else str(row[4] or ""),
            }
        )
    return jsonify({"materials": materials})


@video_shorts_bp.route("/api/interview/interviews/<interview_id>/materials/import", methods=["POST"])
def interview_materials_import_api(interview_id: str):
    user_id = _require_user_id()
    if not user_id:
        return jsonify({"error": "auth required"}), 401

    payload = request.get_json(silent=True) or {}
    raw_text = str(payload.get("text") or request.form.get("text") or "")
    replace_existing = bool(payload.get("replace_existing"))
    if not raw_text.strip():
        return jsonify({"error": "text is required"}), 400

    pairs = _parse_qa_pairs(raw_text)
    if not pairs:
        return jsonify({"error": "No Q/A pairs parsed. Use lines starting with Q:/A: (or Soru:/Cevap:)."}), 400

    _ensure_schema_with_rw()
    conn = get_db()
    try:
        ensure_interview_practice_schema(conn)
        interview = _load_interview_detail(conn, user_id=user_id, interview_id=interview_id)
        if not interview:
            return jsonify({"error": "interview not found"}), 404

        if replace_existing:
            conn.execute(
                "DELETE FROM int_interview_materials WHERE interview_id = ? AND user_id = ?",
                [interview_id, user_id],
            )

        row = conn.execute(
            "SELECT COALESCE(MAX(sort_order), -1) FROM int_interview_materials WHERE interview_id = ? AND user_id = ?",
            [interview_id, user_id],
        ).fetchone()
        start_order = int((row[0] if row else -1) or -1) + 1

        now = datetime.utcnow()
        for idx, pair in enumerate(pairs):
            conn.execute(
                """
                INSERT INTO int_interview_materials (
                    id, interview_id, user_id, sort_order, question, answer, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    str(uuid4()),
                    interview_id,
                    user_id,
                    start_order + idx,
                    pair["question"],
                    pair["answer"],
                    now,
                ],
            )
        conn.commit()
    finally:
        conn.close()

    return jsonify({"ok": True, "imported_count": len(pairs), "replace_existing": replace_existing})


@video_shorts_bp.route("/api/interview/interviews/<interview_id>/tags", methods=["POST"])
def interview_add_tag_api(interview_id: str):
    user_id = _require_user_id()
    if not user_id:
        return jsonify({"error": "auth required"}), 401

    payload = request.get_json(silent=True) or {}
    tag_name = str(payload.get("tag") or request.form.get("tag") or "").strip()
    if not tag_name:
        return jsonify({"error": "tag is required"}), 400

    _ensure_schema_with_rw()
    conn = get_db()
    try:
        ensure_interview_practice_schema(conn)
        interview = _load_interview_detail(conn, user_id=user_id, interview_id=interview_id)
        if not interview:
            return jsonify({"error": "interview not found"}), 404

        existing = conn.execute(
            "SELECT id, name FROM int_tags WHERE user_id = ? AND lower(name) = lower(?)",
            [user_id, tag_name],
        ).fetchone()
        if existing:
            tag_id = str(existing[0])
            normalized_name = str(existing[1] or tag_name)
        else:
            tag_id = str(uuid4())
            normalized_name = tag_name
            conn.execute(
                "INSERT INTO int_tags (id, user_id, name, created_at) VALUES (?, ?, ?, ?)",
                [tag_id, user_id, normalized_name, datetime.utcnow()],
            )

        exists_link = conn.execute(
            "SELECT 1 FROM int_interview_tags WHERE interview_id = ? AND tag_id = ?",
            [interview_id, tag_id],
        ).fetchone()
        if not exists_link:
            conn.execute(
                "INSERT INTO int_interview_tags (interview_id, tag_id, created_at) VALUES (?, ?, ?)",
                [interview_id, tag_id, datetime.utcnow()],
            )
            conn.commit()
    finally:
        conn.close()

    return jsonify({"ok": True, "tag": {"id": tag_id, "name": normalized_name}})


@video_shorts_bp.route("/api/interview/interviews/<interview_id>/tags/<tag_id>", methods=["DELETE"])
def interview_remove_tag_api(interview_id: str, tag_id: str):
    user_id = _require_user_id()
    if not user_id:
        return jsonify({"error": "auth required"}), 401

    _ensure_schema_with_rw()
    conn = get_db()
    try:
        ensure_interview_practice_schema(conn)
        interview = _load_interview_detail(conn, user_id=user_id, interview_id=interview_id)
        if not interview:
            return jsonify({"error": "interview not found"}), 404

        tag_row = conn.execute(
            "SELECT id FROM int_tags WHERE id = ? AND user_id = ?",
            [tag_id, user_id],
        ).fetchone()
        if not tag_row:
            return jsonify({"error": "tag not found"}), 404

        conn.execute(
            "DELETE FROM int_interview_tags WHERE interview_id = ? AND tag_id = ?",
            [interview_id, tag_id],
        )
        conn.commit()
    finally:
        conn.close()

    return jsonify({"ok": True})


@video_shorts_bp.route("/api/interview/interviews/<interview_id>/recordings", methods=["POST"])
def interview_recordings_create_api(interview_id: str):
    user_id = _require_user_id()
    if not user_id:
        return jsonify({"error": "auth required"}), 401

    _ensure_schema_with_rw()
    audio_file = request.files.get("audio")
    if not audio_file:
        return jsonify({"error": "audio file is required"}), 400

    raw_audio_bytes = audio_file.read()
    if not raw_audio_bytes:
        return jsonify({"error": "audio file is empty"}), 400

    mime_type = str(audio_file.mimetype or "audio/webm")
    audio_bytes = raw_audio_bytes
    transcoded_bytes, transcoded_mime = _transcode_audio_to_mp3(raw_audio_bytes, mime_type)
    if transcoded_bytes:
        audio_bytes = transcoded_bytes
        mime_type = transcoded_mime
    note = str(request.form.get("note") or "").strip()
    transcript = str(request.form.get("transcript") or "").strip()
    secondary_text = str(request.form.get("secondary_text") or "").strip()

    conn = get_db()
    try:
        ensure_interview_practice_schema(conn)
        interview = _load_interview_detail(conn, user_id=user_id, interview_id=interview_id)
        if not interview:
            return jsonify({"error": "interview not found"}), 404

        recording_id = str(uuid4())
        now = datetime.utcnow()
        conn.execute(
            """
            INSERT INTO int_recordings (
                id, interview_id, user_id, note, transcript, secondary_text, mime_type, audio_blob, created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                recording_id,
                interview_id,
                user_id,
                note,
                transcript,
                secondary_text,
                mime_type,
                audio_bytes,
                now,
            ],
        )
        conn.commit()
    finally:
        conn.close()

    return jsonify(
        {
            "ok": True,
            "recording": {
                "id": recording_id,
                "note": note,
                "transcript": transcript,
                "secondary_text": secondary_text,
                "mime_type": mime_type,
                "created_at": now.isoformat(),
                "audio_url": url_for("video_shorts_bp.interview_recording_audio_api", recording_id=recording_id),
            },
        }
    )


@video_shorts_bp.route("/api/interview/recordings/<recording_id>", methods=["DELETE"])
def interview_recordings_delete_api(recording_id: str):
    user_id = _require_user_id()
    if not user_id:
        return jsonify({"error": "auth required"}), 401

    _ensure_schema_with_rw()
    conn = get_db()
    try:
        ensure_interview_practice_schema(conn)
        exists = conn.execute(
            "SELECT 1 FROM int_recordings WHERE id = ? AND user_id = ?",
            [recording_id, user_id],
        ).fetchone()
        if not exists:
            return jsonify({"error": "recording not found"}), 404

        conn.execute("DELETE FROM int_recordings WHERE id = ? AND user_id = ?", [recording_id, user_id])
        conn.commit()
    finally:
        conn.close()

    return jsonify({"ok": True})


@video_shorts_bp.route("/api/interview/recordings/<recording_id>/audio", methods=["GET"])
def interview_recording_audio_api(recording_id: str):
    user_id = _require_user_id()
    if not user_id:
        return Response(status=401)

    _ensure_schema_with_rw()
    conn = get_db_readonly()
    try:
        row = conn.execute(
            "SELECT audio_blob, mime_type FROM int_recordings WHERE id = ? AND user_id = ?",
            [recording_id, user_id],
        ).fetchone()
    finally:
        conn.close()

    if not row:
        return Response(status=404)

    raw_blob = row[0]
    if isinstance(raw_blob, memoryview):
        audio_bytes = raw_blob.tobytes()
    elif isinstance(raw_blob, (bytes, bytearray)):
        audio_bytes = bytes(raw_blob)
    else:
        audio_bytes = bytes(raw_blob or b"")

    mime_type = str(row[1] or "audio/webm")
    # Backward compatibility: convert old non-seekable recordings to MP3 on delivery.
    if mime_type.lower() not in {"audio/mpeg", "audio/mp3"}:
        transcoded_bytes, transcoded_mime = _transcode_audio_to_mp3(audio_bytes, mime_type)
        if transcoded_bytes:
            audio_bytes = transcoded_bytes
            mime_type = transcoded_mime
    total_size = len(audio_bytes)
    range_header = request.headers.get("Range") or ""
    headers = {"Accept-Ranges": "bytes"}

    # Support byte range requests so browser player can seek reliably.
    if range_header.startswith("bytes=") and total_size > 0:
        match = re.match(r"bytes=(\d*)-(\d*)", range_header.strip())
        if match:
            start_raw, end_raw = match.groups()
            try:
                if start_raw == "" and end_raw:
                    length = min(int(end_raw), total_size)
                    start = max(total_size - length, 0)
                    end = total_size - 1
                else:
                    start = int(start_raw) if start_raw else 0
                    end = int(end_raw) if end_raw else (total_size - 1)
                    if start > end:
                        start, end = end, start
                    start = max(start, 0)
                    end = min(end, total_size - 1)

                if start < total_size:
                    chunk = audio_bytes[start : end + 1]
                    headers["Content-Range"] = f"bytes {start}-{end}/{total_size}"
                    headers["Content-Length"] = str(len(chunk))
                    return Response(chunk, status=206, mimetype=mime_type, headers=headers)
            except Exception:
                pass

    headers["Content-Length"] = str(total_size)
    return Response(audio_bytes, status=200, mimetype=mime_type, headers=headers)


@video_shorts_bp.route("/api/interview/cleanup", methods=["POST"])
def interview_cleanup_placeholder():
    user_id = _require_user_id()
    if not user_id:
        return jsonify({"error": "auth required"}), 401

    payload = request.get_json(silent=True) or {}
    transcript = (payload.get("transcript") or "").strip()
    if not transcript:
        return jsonify({"error": "transcript is required"}), 400

    return jsonify(
        {
            "ok": True,
            "placeholder": True,
            "message": "Cleanup endpoint is a placeholder for future LLM processing.",
            "goals": {
                "duration_target": "shorten to ~60 seconds",
                "filler_words": "remove filler words",
                "tone": "make recruiter-friendly",
            },
            "cleaned_text": transcript,
        }
    )


@video_shorts_bp.route("/api/interview/system-audio-transcribe", methods=["POST"])
def interview_system_audio_transcribe():
    user_id = _require_user_id()
    if not user_id:
        return jsonify({"error": "auth required"}), 401

    if _openai_client is None:
        return jsonify({"error": "Speech transcription service is not configured."}), 503

    audio_file = request.files.get("audio")
    if not audio_file:
        return jsonify({"error": "audio file is required"}), 400

    suffix = Path(audio_file.filename or "capture.webm").suffix or ".webm"
    temp_path = None
    try:
        with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as temp:
            audio_file.save(temp)
            temp_path = Path(temp.name)

        with temp_path.open("rb") as f:
            resp = _openai_client.audio.transcriptions.create(
                model=WHISPER_MODEL,
                file=f,
                response_format="verbose_json",
                prompt="Transcribe spoken language exactly. Keep punctuation natural.",
            )

        text = getattr(resp, "text", None)
        language = getattr(resp, "language", None)
        if text is None and isinstance(resp, dict):
            text = resp.get("text")
            language = language or resp.get("language")
        if not isinstance(text, str):
            text = ""

        return jsonify(
            {
                "ok": True,
                "text": text.strip(),
                "language": (language or "").strip().lower(),
            }
        )
    except Exception as exc:
        return jsonify({"error": f"System audio transcription failed: {exc}"}), 500
    finally:
        if temp_path and temp_path.exists():
            try:
                temp_path.unlink()
            except Exception:
                pass
