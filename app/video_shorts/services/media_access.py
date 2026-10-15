"""Ownership checks shared by playback, publishing, and social workers."""
from __future__ import annotations

import json
from pathlib import Path

from flask import current_app, has_request_context, url_for
from itsdangerous import URLSafeTimedSerializer

from app.video_shorts.services import tenant_media
from app.video_shorts.services.db import get_db_readonly, table_columns

MEDIA_TOKEN_MAX_AGE = 600


def authorize_file(filename, *, owner_user_id, brand_id, video_pk=None, require_lifecycle=True):
    name = tenant_media.safe_filename(filename)
    if Path(name).suffix.lower() not in {".mp4", ".mov", ".mkv", ".webm"} or not owner_user_id:
        raise tenant_media.TenantMediaConflict("Media ownership is required")
    owner = str(owner_user_id)
    brand = str(brand_id) if brand_id else None
    conn = get_db_readonly()
    try:
        rows = conn.execute(
            """SELECT user_id, brand_id, source_video_id, storage_file_key, raw_plan_entry_json
               FROM shorts_generated_videos WHERE clip_filename = ? OR output_filename = ?""",
            [name, name],
        ).fetchall()
        if not rows and name.startswith("ai_") and table_columns(conn, "shorts_ai_videos"):
            rows = conn.execute(
                """SELECT user_id, brand_id, '', ?, NULL FROM shorts_ai_videos
                   WHERE clip_filename = ?""", [tenant_media.asset_key(name), name],
            ).fetchall()
        compilation = None
        if name.startswith(("long_from_shorts_", "monthly_top_")) and table_columns(conn, "shorts_compilations"):
            compilation = conn.execute(
                """SELECT owner_user_id, brand_id, video_pk, storage_file_key
                   FROM shorts_compilations WHERE filename = ?""", [name],
            ).fetchone()
        if require_lifecycle and not rows and not compilation:
            raise tenant_media.TenantMediaConflict("Media has no stored ownership record")
        identities = [(str(r[0] or ""), str(r[1]) if r[1] else None) for r in rows]
        if compilation:
            identities.append((str(compilation[0]), str(compilation[1]) if compilation[1] else None))
        if any(identity != (owner, brand) for identity in identities):
            raise tenant_media.TenantMediaConflict("Media belongs to another tenant")
        keys = {str(r[3] or tenant_media.asset_key(name)) for r in rows}
        if compilation:
            keys.add(str(compilation[3]))
        if not keys:
            keys.add(tenant_media.asset_key(name))
        if len(keys) != 1:
            raise tenant_media.TenantMediaConflict("Media storage identity is ambiguous")
        asset_key = keys.pop()
        asset = conn.execute(
            "SELECT user_id, brand_id, file_path FROM shorts_storage_assets WHERE file_key = ?", [asset_key],
        ).fetchone()
        if not asset or (str(asset[0] or ""), str(asset[1]) if asset[1] else None) != (owner, brand):
            raise tenant_media.TenantMediaConflict("Media asset owner/brand does not match")
        if video_pk is not None:
            video = tenant_media.resolve_video(video_pk=video_pk, owner_user_id=owner, brand_id=brand)
            if compilation:
                if int(compilation[2]) != video.video_pk:
                    raise tenant_media.TenantMediaConflict("Compilation belongs to another video")
            elif not rows or any(str(r[2]) != video.youtube_id for r in rows):
                raise tenant_media.TenantMediaConflict("Clip belongs to another video")
        # Explicit lifecycle + asset ownership is authoritative for an individual
        # clip. Only an asset-only legacy fallback needs the source's frozen owner;
        # a shared legacy plan could contain distinct clips rendered by two tenants.
        import re
        legacy = re.fullmatch(r"\d+_([A-Za-z0-9_-]+)\.mp4", name)
        if legacy and not rows and not re.search(r"_v\d+\.mp4$", name):
            frozen = conn.execute(
                """SELECT v.owner_user_id, v.brand_id FROM shorts_video_legacy_owners l
                   JOIN youtube_videos v ON v.id = l.video_pk WHERE l.youtube_id = ?""", [legacy[1]],
            ).fetchone()
            if not frozen or (str(frozen[0] or ""), str(frozen[1]) if frozen[1] else None) != (owner, brand):
                raise tenant_media.TenantMediaConflict("Legacy media is reserved for its frozen owner")
        location = tenant_media.storage_location(asset[2])
        storage_key = location["storage_key"]
        posters = {name + ".jpg", Path(name).stem + ".jpg"}
        for row in rows:
            raw = row[4]
            try:
                entry = raw if isinstance(raw, dict) else json.loads(raw or "{}")
                if entry.get("poster_filename"):
                    posters.add(tenant_media.safe_filename(entry["poster_filename"]))
            except (TypeError, ValueError):
                pass
        return {"filename": name, "storage_key": storage_key, "local_path": location["local_path"],
                "storage_file_key": asset_key, "owner": owner, "brand": brand,
                "posters": posters, "source_ids": {str(r[2]) for r in rows}}
    finally:
        conn.close()


def authorize_plan_clip(video_pk, filename, *, plan_index=None):
    owner, brand = tenant_media.current_scope()
    video = tenant_media.resolve_video(video_pk=video_pk, owner_user_id=owner, brand_id=brand)
    data = tenant_media.load_state(video)
    entries = data.get("plan") or data.get("clips") or []
    for entry in entries:
        if plan_index not in (None, "") and str(entry.get("plan_index")) != str(plan_index):
            continue
        stored_name = entry.get("output_filename") or entry.get("clip_filename")
        if stored_name == filename:
            authorize_file(filename, owner_user_id=owner, brand_id=brand, video_pk=video.video_pk)
            conn = get_db_readonly()
            try:
                tenant_media.assert_clip_write_owner(conn, filename, owner, brand,
                                                     video_pk=video.video_pk, source_id=video.youtube_id)
            finally:
                conn.close()
            return video, entry
    raise tenant_media.TenantMediaConflict("Filename is not the stored clip of this plan entry")


def _serializer():
    return URLSafeTimedSerializer(current_app.secret_key, salt="tenant-short-media-v1")


def share_scope(token, filename):
    from app.video_shorts.routes.generation import _load_shared_short_row
    conn = get_db_readonly()
    try:
        shared = _load_shared_short_row(conn, token)
        if not shared or shared.get("clip_filename") != filename:
            raise tenant_media.TenantMediaConflict("Share token does not grant this clip")
        row = conn.execute("SELECT user_id, brand_id FROM shorts_generated_videos WHERE id = ?",
                           [shared["generated_video_id"]]).fetchone()
        if not row:
            raise tenant_media.TenantMediaConflict("Shared clip no longer exists")
        return str(row[0]), str(row[1]) if row[1] else None
    finally:
        conn.close()


def media_url(filename, *, poster_filename=None, share_token=None):
    if not filename or not has_request_context():
        return ""
    try:
        owner, brand = share_scope(share_token, filename) if share_token else tenant_media.current_scope()
        info = authorize_file(filename, owner_user_id=owner, brand_id=brand, require_lifecycle=False)
        if poster_filename and poster_filename not in info["posters"]:
            # The live plan may have a newer poster than the lifecycle snapshot.
            for source_id in info["source_ids"]:
                video = tenant_media.resolve_video(source_id, owner_user_id=owner, brand_id=brand)
                for entry in (tenant_media.load_state(video).get("plan") or []):
                    if filename in (entry.get("clip_filename"), entry.get("output_filename")):
                        if entry.get("poster_filename"):
                            info["posters"].add(tenant_media.safe_filename(entry["poster_filename"]))
            if poster_filename not in info["posters"]:
                return ""
        grant = {"filename": filename, "owner": owner, "brand": brand, "poster": poster_filename}
        if share_token:
            grant["share"] = share_token
        return url_for("video_shorts_bp.serve_short_media", filename=filename, grant=_serializer().dumps(grant))
    except (ValueError, TypeError):
        return ""


def read_media_grant(filename, grant):
    data = _serializer().loads(grant, max_age=MEDIA_TOKEN_MAX_AGE)
    if data.get("filename") != filename:
        raise tenant_media.TenantMediaConflict("Media grant is for another file")
    if data.get("share"):
        owner, brand = share_scope(data["share"], filename)
        if (owner, brand) != (data.get("owner"), data.get("brand")):
            raise tenant_media.TenantMediaConflict("Share ownership changed")
    info = authorize_file(filename, owner_user_id=data.get("owner"), brand_id=data.get("brand"), require_lifecycle=False)
    # Poster names are included in the signed grant only after checking the plan.
    if data.get("poster"):
        info["storage_key"] = tenant_media.poster_key(tenant_media.safe_filename(data["poster"]))
        info["local_path"] = tenant_media.storage_location(info["storage_key"])["local_path"]
    return info


def authorize_queue_clip(job):
    from app.video_shorts.services.social_queue_identity import queue_identity
    owner, brand = queue_identity(job.get("user_id"), job.get("brand_id"))
    info = authorize_file(job.get("clip_filename"), owner_user_id=owner, brand_id=brand)
    conn = get_db_readonly()
    try:
        tenant_media.assert_clip_write_owner(conn, info["filename"], owner, brand)
    finally:
        conn.close()
    return info


def worker_clip_path(job):
    from app.video_shorts.services.storage import get_media_storage
    info = authorize_queue_clip(job)
    local = tenant_media.short_path(info["filename"])
    if local.is_file():
        return local
    return get_media_storage().download_to_temp(info["storage_key"])


def preview_url(video_id, filename):
    if not has_request_context():
        return ""
    try:
        owner, brand = tenant_media.current_scope()
        name = tenant_media.safe_filename(filename)
        _authorize_preview(video_id, name, owner, brand)
        grant = _serializer().dumps({"source": video_id, "filename": name, "owner": owner, "brand": brand, "kind": "preview"})
        return url_for("video_shorts_bp.serve_short_preview", filename=name, grant=grant)
    except (ValueError, TypeError):
        return ""


def _authorize_preview(video_id, filename, owner, brand):
    if not owner or not filename.startswith(str(video_id) + "_") or not filename.endswith(".jpg"):
        raise tenant_media.TenantMediaConflict("Preview ownership is required")
    if str(video_id).startswith("i2v_"):
        conn = get_db_readonly()
        try:
            row = conn.execute("SELECT user_id, brand_id FROM image_to_video_jobs WHERE CAST(job_id AS VARCHAR) = ?", [str(video_id)[4:]]).fetchone()
            if not row or (str(row[0]), str(row[1]) if row[1] else None) != (str(owner), brand):
                raise tenant_media.TenantMediaConflict("Preview belongs to another tenant")
        finally:
            conn.close()
    else:
        tenant_media.resolve_video(video_id, owner_user_id=owner, brand_id=brand)


def read_preview_grant(filename, grant):
    data = _serializer().loads(grant, max_age=MEDIA_TOKEN_MAX_AGE)
    if data.get("kind") != "preview" or data.get("filename") != filename:
        raise tenant_media.TenantMediaConflict("Invalid preview grant")
    _authorize_preview(data.get("source"), filename, data.get("owner"), data.get("brand"))
    return tenant_media.safe_filename(filename)
