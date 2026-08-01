import json
import threading
from datetime import datetime
from uuid import uuid4

from flask import current_app, g, jsonify, redirect, render_template, request, url_for

from app.video_shorts import video_shorts_bp
from app.video_shorts.routes.settings import _user_image_public_url
from app.video_shorts.services.brands import current_brand_id, ensure_brand_schema
from app.video_shorts.services.db import (
    ensure_image_to_video_jobs_schema,
    ensure_static_images_schema,
    get_db,
    get_db_readonly,
)
from app.video_shorts.services.image_to_video import render_job_from_db
from app.video_shorts.services.storage import is_storage_reference, public_url_for_stored_media


@video_shorts_bp.route("/image-to-video")
def image_to_video_lab():
    current_user = getattr(g, "vs_current_user", None)
    if not current_user:
        return redirect(url_for("video_shorts_bp.login", next=request.url))
    return render_template("image_to_video_lab.html")


@video_shorts_bp.route("/api/static-images", methods=["GET"])
def list_static_images():
    current_user = getattr(g, "vs_current_user", None)
    if not current_user:
        return jsonify({"error": "auth required"}), 401

    user_id = current_user.get("id")
    brand_id = current_brand_id()
    conn = get_db_readonly()
    try:
        rows = conn.execute(
            """
            SELECT id, label, filename, created_at
            FROM shorts_static_images
            WHERE user_id = ? AND brand_id = ? AND COALESCE(is_active, true) = true
              AND COALESCE(asset_kind, 'background') = 'background'
            ORDER BY created_at DESC
            """,
            [user_id, brand_id],
        ).fetchall()
    finally:
        conn.close()

    images = [
        {
            "id": str(row[0]),
            "label": row[1] or "",
            "filename": row[2] or "",
            "created_at": row[3].isoformat() if getattr(row[3], "isoformat", None) else str(row[3] or ""),
            "url": _user_image_public_url(user_id, str(row[2] or "")),
        }
        for row in rows
        if row[2]
    ]
    return jsonify({"images": images})


def _serialize_image_to_video_job(row):
    payload = {}
    try:
        payload = json.loads(row[2] or "{}")
    except Exception:
        payload = {}
    output_url = str(row[5] or "")
    if output_url and is_storage_reference(output_url):
        output_url = public_url_for_stored_media(output_url, fallback_local_url="") or output_url
    created_at = row[7]
    updated_at = row[8]
    return {
        "job_id": str(row[0]),
        "image_ids": payload.get("image_ids") or [],
        "payload": payload,
        "status": str(row[3] or "queued"),
        "progress": int(row[4] or 0),
        "output_url": output_url,
        "error_message": str(row[6] or ""),
        "created_at": created_at.isoformat() if getattr(created_at, "isoformat", None) else str(created_at or ""),
        "updated_at": updated_at.isoformat() if getattr(updated_at, "isoformat", None) else str(updated_at or ""),
    }


def _run_image_to_video_job(job_id: str, app) -> None:
    with app.app_context():
        render_job_from_db(job_id)


@video_shorts_bp.route("/api/image-to-video/jobs", methods=["GET"])
def list_image_to_video_jobs():
    current_user = getattr(g, "vs_current_user", None)
    if not current_user:
        return jsonify({"error": "auth required"}), 401

    user_id = current_user.get("id")
    brand_id = current_brand_id()
    conn = get_db_readonly()
    try:
        ensure_image_to_video_jobs_schema(conn)
        rows = conn.execute(
            """
            SELECT job_id, image_ids_json, payload_json, status, progress, output_url, error_message, created_at, updated_at
            FROM image_to_video_jobs
            WHERE user_id = ? AND brand_id = ?
            ORDER BY created_at DESC
            """,
            [user_id, brand_id],
        ).fetchall()
    finally:
        conn.close()
    return jsonify({"jobs": [_serialize_image_to_video_job(row) for row in rows]})


@video_shorts_bp.route("/api/image-to-video/jobs", methods=["POST"])
def create_image_to_video_job():
    current_user = getattr(g, "vs_current_user", None)
    if not current_user:
        return jsonify({"error": "auth required"}), 401

    payload = request.get_json(silent=True) or {}
    image_ids = payload.get("image_ids") or []
    if not isinstance(image_ids, list) or not image_ids:
        return jsonify({"error": "image_ids required"}), 400

    user_id = current_user.get("id")
    brand_id = current_brand_id()
    conn = get_db()
    try:
        ensure_brand_schema(conn)
        ensure_static_images_schema(conn)
        ensure_image_to_video_jobs_schema(conn)
        rows = conn.execute(
            """
            SELECT id
            FROM shorts_static_images
            WHERE user_id = ? AND brand_id = ? AND COALESCE(is_active, true) = true
              AND id IN ({})
            """.format(",".join(["?"] * len(image_ids))),
            [user_id, brand_id, *[str(image_id) for image_id in image_ids]],
        ).fetchall()
        allowed_ids = {str(row[0]) for row in rows}
        if len(allowed_ids) != len({str(image_id) for image_id in image_ids}):
            return jsonify({"error": "one or more images are not available in the active brand"}), 400

        job_id = str(uuid4())
        now = datetime.utcnow()
        conn.execute(
            """
            INSERT INTO image_to_video_jobs (
                job_id, user_id, brand_id, image_ids_json, payload_json, status, progress, output_url, error_message, created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                job_id,
                user_id,
                brand_id,
                json.dumps(image_ids),
                json.dumps(payload),
                "queued",
                0,
                "",
                "",
                now,
                now,
            ],
        )
        conn.commit()
    finally:
        conn.close()

    thread = threading.Thread(
        target=_run_image_to_video_job,
        args=(job_id, current_app._get_current_object()),
        daemon=True,
    )
    thread.start()
    return jsonify(
        {
            "job_id": job_id,
            "status": "queued",
            "progress": 0,
            "output_url": "",
            "created_at": now.isoformat(),
        }
    )


@video_shorts_bp.route("/api/image-to-video/jobs/<job_id>", methods=["GET"])
def get_image_to_video_job(job_id: str):
    current_user = getattr(g, "vs_current_user", None)
    if not current_user:
        return jsonify({"error": "auth required"}), 401

    user_id = current_user.get("id")
    brand_id = current_brand_id()
    conn = get_db_readonly()
    try:
        ensure_image_to_video_jobs_schema(conn)
        row = conn.execute(
            """
            SELECT job_id, image_ids_json, payload_json, status, progress, output_url, error_message, created_at, updated_at
            FROM image_to_video_jobs
            WHERE job_id = ? AND user_id = ? AND brand_id = ?
            """,
            [job_id, user_id, brand_id],
        ).fetchone()
    finally:
        conn.close()
    if not row:
        return jsonify({"error": "job not found"}), 404
    return jsonify(_serialize_image_to_video_job(row))


@video_shorts_bp.route("/api/image-to-video/jobs/<job_id>", methods=["DELETE"])
def delete_image_to_video_job(job_id: str):
    current_user = getattr(g, "vs_current_user", None)
    if not current_user:
        return jsonify({"error": "auth required"}), 401

    user_id = current_user.get("id")
    brand_id = current_brand_id()
    conn = get_db()
    try:
        ensure_image_to_video_jobs_schema(conn)
        result = conn.execute(
            """
            DELETE FROM image_to_video_jobs
            WHERE job_id = ? AND user_id = ? AND brand_id = ?
            """,
            [job_id, user_id, brand_id],
        )
        conn.commit()
    finally:
        conn.close()
    if result.rowcount == 0:
        return jsonify({"error": "job not found"}), 404
    return jsonify({"ok": True})
