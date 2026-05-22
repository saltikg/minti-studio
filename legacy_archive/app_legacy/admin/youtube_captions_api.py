# app/admin/youtube_captions_api.py
import os
from flask import request, jsonify, current_app
from app.admin import admin_bp
from app.db import connect_ro, connect_rw

# Simdilik basit ve net olsun, hem burada hem local worker da ayni stringi kullaniyoruz
CAPTION_API_TOKEN = "minti_caption_8273f4ac0b"



def _get_db(read_only=True):
    return connect_ro() if read_only else connect_rw()

def _check_token():
    token = request.headers.get("X-Api-Token")
    return bool(token and token == CAPTION_API_TOKEN)


# 1) Altyazisi olmayan videolar icin gorev listesi
@admin_bp.route("/api/youtube-caption-tasks", methods=["GET"])
def api_youtube_caption_tasks():
    if not _check_token():
        return jsonify({"error": "forbidden"}), 403

    try:
        limit = int(request.args.get("limit", "20"))
    except ValueError:
        limit = 20

    con = _get_db(True)
    rows = con.execute(
        """
        SELECT id, video_id, video_title, video_url
        FROM youtube_videos
        WHERE has_captions = FALSE
        ORDER BY published_at DESC
        LIMIT ?
        """,
        [limit],
    ).fetchall()
    cols = [d[0] for d in con.description]
    videos = [dict(zip(cols, r)) for r in rows]
    con.close()

    return jsonify({"tasks": videos})


# 2) Local worker dan gelen caption sonucunu kaydet
@admin_bp.route("/api/youtube-caption-result", methods=["POST"])
def api_youtube_caption_result():
    if not _check_token():
        return jsonify({"error": "forbidden"}), 403

    data = request.get_json(silent=True) or {}
    video_db_id = data.get("video_db_id")
    caption_text = (data.get("caption_text") or "").strip()
    lang = (data.get("lang") or "en").strip()

    if not video_db_id or not caption_text:
        return jsonify({"error": "missing fields"}), 400

    con = _get_db(False)
    try:
        # caption ekle
        con.execute(
            """
            INSERT INTO youtube_captions
              (video_id, caption_text, source, lang)
            VALUES (?, ?, 'local_worker', ?)
            """,
            [video_db_id, caption_text, lang],
        )

        # video durumunu guncelle
        con.execute(
            """
            UPDATE youtube_videos
            SET has_captions = TRUE,
                caption_lang = ?,
                last_checked_at = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            [lang, video_db_id],
        )

        con.commit()
        con.close()
        return jsonify({"ok": True}), 200
    except Exception as e:
        current_app.logger.exception("caption result save failed")
        con.close()
        return jsonify({"error": str(e)}), 500
