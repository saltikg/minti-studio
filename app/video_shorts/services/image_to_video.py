import math
import logging
import os
import subprocess
from contextlib import ExitStack
from dataclasses import dataclass
from pathlib import Path
import tempfile
import json
import time
from typing import List, Optional
import sys
import argparse

from app.video_shorts.config import (
    FFMPEG_TIMEOUT,
    STATIC_USER_AUDIO_DIR,
    STATIC_USER_IMAGES_DIR,
    VIDEOS_DIR,
)
from app.video_shorts.services.db import get_db, ensure_image_to_video_jobs_schema, table_columns
from app.video_shorts.services.media_utils import _resolve_ffmpeg
from app.video_shorts.services.storage import build_storage_reference, get_media_storage

TRANSITIONS_DIR = Path(__file__).resolve().parent.parent / "static" / "transitions"
WATERCOLOR_INK_MATTE_DIR = TRANSITIONS_DIR / "watercolor_ink_matte"
# Sample a later matte segment to avoid initial white-pop frames.
WATERCOLOR_MATTE_START_SECONDS = 0.35
WATERCOLOR_MATTE_TRANSITION_SECONDS = 2.0

logger = logging.getLogger(__name__)


@dataclass
class RenderSpec:
    width: int
    height: int
    fps: int
    duration_mode: str
    total_duration: Optional[float]
    per_image: Optional[float]
    image_duration_sequence: List[Optional[float]]
    motion_preset: str
    transition_motion_sequence: List[str]
    transition_type_sequence: List[str]
    transition_duration_sequence: List[Optional[float]]
    style_preset: str
    transition: str
    overlay: dict
    music_filename: str
    music_volume: float
    music_start_seconds: float
    debug_transitions: bool


def _aspect_resolution(aspect_ratio: str, resolution: str) -> tuple[int, int]:
    res = 1080 if resolution == "1080p" else 720
    if aspect_ratio == "16:9":
        return (1920 if res == 1080 else 1280, res)
    if aspect_ratio == "1:1":
        return (res, res)
    # default 9:16
    return (res, 1920 if res == 1080 else 1280)


def _duration_plan(count: int, spec: RenderSpec) -> List[float]:
    if count <= 0:
        return []
    if spec.image_duration_sequence:
        out: List[float] = []
        for idx in range(count):
            raw = spec.image_duration_sequence[idx] if idx < len(spec.image_duration_sequence) else None
            try:
                value = float(raw) if raw is not None else 5.0
            except (TypeError, ValueError):
                value = 5.0
            out.append(max(value, 0.5))
        return out
    if spec.duration_mode == "total" and spec.total_duration:
        per = max(spec.total_duration / count, 0.5)
        return [per for _ in range(count)]
    if spec.duration_mode == "per_image" and spec.per_image:
        return [max(spec.per_image, 0.5) for _ in range(count)]
    # auto
    if count == 1:
        return [6.0]
    return [2.5 for _ in range(count)]


def _motion_filter(preset: str, width: int, height: int, frames: int, fps: int) -> str:
    # Preserve full image content: fit inside target frame, then letterbox/pillarbox.
    # This avoids unintended top/bottom cropping on tall or wide source images.
    base = (
        f"scale={width}:{height}:force_original_aspect_ratio=decrease,"
        f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:color=black"
    )
    if not preset:
        return base

    speed = 0.0012
    zoom = 1.08
    if preset in {"documentary"}:
        speed = 0.0006
        zoom = 1.04
    elif preset in {"cinematic"}:
        speed = 0.001
        zoom = 1.1
    elif preset in {"dynamic"}:
        speed = 0.0018
        zoom = 1.12

    span = max(frames - 1, 1)

    if preset in {"zoom_out"}:
        zoom = 1.12
        speed = 0.0015
        z_expr = f"max(1.0,{zoom}-({speed}*on))"
    elif preset in {"zoom_in_right"}:
        zoom = 1.12
        speed = 0.0014
        z_expr = f"min(1.02+({speed}*on),{zoom})"
    elif preset in {"zoom_in_left"}:
        zoom = 1.12
        speed = 0.0014
        z_expr = f"min(1.02+({speed}*on),{zoom})"
    elif preset in {"zoom_out_right"}:
        zoom = 1.12
        speed = 0.0015
        z_expr = f"max(1.0,{zoom}-({speed}*on))"
    elif preset in {"zoom_out_left"}:
        zoom = 1.12
        speed = 0.0015
        z_expr = f"max(1.0,{zoom}-({speed}*on))"
    elif preset in {"pan_left"}:
        z_expr = "1.08"
    elif preset in {"pan_right"}:
        z_expr = "1.08"
    elif preset in {"still"}:
        z_expr = "1.0"
    else:
        z_expr = f"min(zoom+{speed},{zoom})"

    # Pan distance must be computed from zoomed window size.
    # Using (iw-ow) can become zero after pre-crop, which makes pan presets look stuck.
    if preset in {"pan_left"}:
        x_expr = f"(iw-iw/zoom)*(1-on/{span})"
    elif preset in {"pan_right"}:
        x_expr = f"(iw-iw/zoom)*(on/{span})"
    elif preset in {"zoom_in_left"}:
        x_expr = f"(iw-iw/zoom)*(0.85-0.70*on/{span})"
    elif preset in {"zoom_in_right"}:
        x_expr = f"(iw-iw/zoom)*(0.15+0.70*on/{span})"
    elif preset in {"zoom_out_left"}:
        x_expr = f"(iw-iw/zoom)*(0.90-0.75*on/{span})"
    elif preset in {"zoom_out_right"}:
        x_expr = f"(iw-iw/zoom)*(0.10+0.75*on/{span})"
    else:
        x_expr = "iw/2-(iw/zoom/2)"

    y_expr = "ih/2-(ih/zoom/2)"
    # When the input is already a duration-limited image sequence, using d={frames}
    # multiplies frames and can make renders extremely slow (or appear hung).
    # We set d=1 and drive total frame count via the input framerate + -t.
    zoompan = f"zoompan=z='{z_expr}':x='{x_expr}':y='{y_expr}':d=1:s={width}x{height}:fps={fps}"
    return f"{base},{zoompan}"


def _effective_motion_for_index(spec: RenderSpec, idx: int, total_count: int) -> str:
    if spec.transition_motion_sequence and idx < len(spec.transition_motion_sequence):
        preset = (spec.transition_motion_sequence[idx] or "").strip()
        if preset:
            return preset
    return spec.motion_preset or ""


def _transition_name(value: str) -> str:
    key = (value or "").strip().lower()
    if key in {"cut", ""}:
        return "cut"
    if key in {
        "watercolor_ink_matte",
        "watercolor-ink-matte",
        "watercolor ink matte",
        "ink_matte",
        "ink-matte",
        "ink matte",
    }:
        return "watercolor_ink_matte"
    if key in {"cross_dissolve", "cross-dissolve", "dissolve"}:
        return "dissolve"
    allowed = {
        "fade",
        "dissolve",
        "wipeleft",
        "wiperight",
        "slideleft",
        "slideright",
        "circleopen",
        "circleclose",
        "smoothleft",
        "smoothright",
    }
    if key in allowed:
        return key
    return "fade"


def _list_watercolor_matte_clips() -> List[Path]:
    if not WATERCOLOR_INK_MATTE_DIR.exists():
        return []
    clips = [p for p in WATERCOLOR_INK_MATTE_DIR.glob("*.mov") if p.is_file()]
    # Prefer Ink_Dark variants first for more stable reveal behavior.
    clips = sorted(clips, key=lambda p: (0 if "dark" in p.stem.lower() else 1, p.name.lower()))
    return clips


def _preferred_watercolor_matte_clip(clips: List[Path]) -> Optional[Path]:
    if not clips:
        return None
    for clip in clips:
        if "dark" in clip.stem.lower():
            return clip
    return clips[0]


def _safe_transition_duration(value: object) -> Optional[float]:
    if value is None:
        return None
    try:
        num = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(num):
        return None
    # Keep xfade stable.
    if num < 0:
        return 0.0
    return min(num, 3.0)


def _safe_image_duration(value: object) -> Optional[float]:
    if value is None:
        return None
    try:
        num = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(num):
        return None
    if num <= 0:
        return None
    return num


def _safe_music_volume(value: object) -> float:
    try:
        num = float(value)
    except (TypeError, ValueError):
        return 0.5
    if not math.isfinite(num):
        return 0.5
    return max(0.0, min(num, 2.0))


def _safe_music_start(value: object) -> float:
    try:
        num = float(value)
    except (TypeError, ValueError):
        return 0.0
    if not math.isfinite(num):
        return 0.0
    return max(0.0, min(num, 36000.0))


def _effective_transition_for_index(
    spec: RenderSpec,
    idx: int,
) -> tuple[str, float]:
    default_name = _transition_name(spec.transition)
    default_duration = 1.0 if default_name != "cut" else 0.0

    name = default_name
    if spec.transition_type_sequence and idx < len(spec.transition_type_sequence):
        seq_name = _transition_name(spec.transition_type_sequence[idx])
        if seq_name:
            name = seq_name

    duration = default_duration
    if name == "cut":
        duration = 0.0
    elif name == "watercolor_ink_matte":
        duration = WATERCOLOR_MATTE_TRANSITION_SECONDS
    else:
        duration = 1.0
    return name, duration


def _style_filter(preset: str) -> str:
    if preset == "historical_sepia":
        return "colorchannelmixer=.393:.769:.189:0:.349:.686:.168:0:.272:.534:.131,eq=contrast=1.05:brightness=0.02:saturation=0.85,vignette"
    if preset == "warm_soft":
        return "eq=contrast=1.03:brightness=0.03:saturation=1.1"
    if preset == "clean_modern":
        return "eq=contrast=1.08:brightness=0.01:saturation=1.02"
    return ""


def _overlay_filters(overlay: dict, width: int, height: int) -> List[str]:
    title = (overlay or {}).get("title") or ""
    subtitle = (overlay or {}).get("subtitle") or ""
    placement = (overlay or {}).get("placement") or "center"

    def _escape_drawtext(value: str) -> str:
        return (
            value.replace("\\", "\\\\")
            .replace(":", "\\:")
            .replace("%", "\\%")
            .replace("'", "\\'")
        )

    if placement == "top":
        y_title = int(height * 0.12)
        y_sub = int(height * 0.2)
    elif placement == "bottom":
        y_title = int(height * 0.75)
        y_sub = int(height * 0.83)
    else:
        y_title = int(height * 0.45)
        y_sub = int(height * 0.55)

    filters = []
    if title:
        filters.append(
            "drawtext=text='{}':fontcolor=white:fontsize={}:x=(w-text_w)/2:y={}:box=1:boxcolor=black@0.35:boxborderw=16".format(
                _escape_drawtext(title),
                int(height * 0.06),
                y_title,
            )
        )
    if subtitle:
        filters.append(
            "drawtext=text='{}':fontcolor=white:fontsize={}:x=(w-text_w)/2:y={}:box=1:boxcolor=black@0.25:boxborderw=12".format(
                _escape_drawtext(subtitle),
                int(height * 0.04),
                y_sub,
            )
        )
    return filters


def render_image_to_video_job(job_id: str, user_id: str, payload: dict, brand_id: Optional[str] = None) -> None:
    temp_inputs: List[Path] = []
    try:
        storage = get_media_storage()
        _safe_update_job(job_id, user_id, status="rendering", progress=5)

        image_ids = payload.get("image_ids") or []
        if not image_ids:
            _safe_fail_job(job_id, user_id, "No images selected")
            return

        conn = get_db()
        try:
            cols = table_columns(conn, "shorts_static_images")
            brand_clause = " AND brand_id = ?" if brand_id and "brand_id" in cols else ""
            params = [user_id]
            rows = conn.execute(
                (
                    "SELECT id, filename FROM shorts_static_images "
                    "WHERE user_id = ?{} AND id IN ({})"
                ).format(
                    brand_clause,
                    ",".join(["?"] * len(image_ids)),
                ),
                params + ([brand_id] if brand_clause else []) + image_ids,
            ).fetchall()
        finally:
            conn.close()

        lookup = {str(r[0]): r[1] for r in rows}
        with ExitStack() as temp_stack:
            ordered_files = []
            for img_id in image_ids:
                filename = lookup.get(str(img_id))
                if not filename:
                    _safe_fail_job(job_id, user_id, "Missing image in library")
                    return
                local_path = STATIC_USER_IMAGES_DIR / user_id / filename
                key = f"user_images/{user_id}/{filename}"
                resolved = storage.resolve_local_or_s3(key, fallback_local_paths=[local_path])
                if not resolved.exists:
                    _safe_fail_job(job_id, user_id, "Image file missing on disk")
                    return
                if resolved.backend == "local" and resolved.local_path:
                    ordered_files.append(resolved.local_path)
                else:
                    ordered_files.append(temp_stack.enter_context(storage.download_to_temp(key)))

            spec = RenderSpec(
                width=0,
                height=0,
                fps=int(payload.get("fps") or 24),
                duration_mode=payload.get("duration_mode") or "auto",
                total_duration=float(payload.get("total_duration_seconds") or 0) or None,
                per_image=float(payload.get("per_image_seconds") or 0) or None,
                image_duration_sequence=[
                    _safe_image_duration(value)
                    for value in (payload.get("image_duration_sequence") or [])
                ],
                motion_preset=payload.get("motion_preset") or "",
                transition_motion_sequence=[
                    str(value or "").strip()
                    for value in (payload.get("transition_motion_sequence") or [])
                ],
                transition_type_sequence=[
                    str(value or "").strip()
                    for value in (payload.get("transition_type_sequence") or [])
                ],
                transition_duration_sequence=[
                    _safe_transition_duration(value)
                    for value in (payload.get("transition_duration_sequence") or [])
                ],
                style_preset=payload.get("style_preset") or "",
                transition=payload.get("transition") or "cut",
                overlay=payload.get("overlay") or {},
                music_filename=str(payload.get("music_filename") or "").strip(),
                music_volume=_safe_music_volume(payload.get("music_volume")),
                music_start_seconds=_safe_music_start(payload.get("music_start_seconds")),
                debug_transitions=bool(payload.get("debug_transitions")),
            )
            spec.width, spec.height = _aspect_resolution(
                payload.get("aspect_ratio") or "9:16",
                payload.get("resolution") or "720p",
            )

            durations = _duration_plan(len(ordered_files), spec)
            if not durations:
                _safe_fail_job(job_id, user_id, "Invalid durations")
                return

            output_dir = VIDEOS_DIR / "image_to_video"
            output_dir.mkdir(parents=True, exist_ok=True)
            output_path = output_dir / f"image_to_video_{job_id}.mp4"

            _safe_update_job(job_id, user_id, status="rendering", progress=25)
            total_duration = sum(durations)
            music_path = None
            if spec.music_filename:
                safe_name = Path(spec.music_filename).name
                if safe_name == spec.music_filename:
                    candidate = STATIC_USER_AUDIO_DIR / user_id / safe_name
                    key = f"user_audio/{user_id}/{safe_name}"
                    resolved = storage.resolve_local_or_s3(key, fallback_local_paths=[candidate])
                    if resolved.exists:
                        if resolved.backend == "local" and resolved.local_path:
                            music_path = resolved.local_path
                        else:
                            music_path = temp_stack.enter_context(storage.download_to_temp(key))
            last_progress = 25.0
            last_tick = time.monotonic()

            def progress_cb(pct: float) -> None:
                nonlocal last_progress, last_tick
                now = time.monotonic()
                if pct < last_progress + 2 and now - last_tick < 2:
                    return
                last_progress = pct
                last_tick = now
                try:
                    _safe_update_job(job_id, user_id, status="rendering", progress=int(pct))
                except Exception:
                    # Don't crash the render if the DB is temporarily locked.
                    pass
            try:
                _run_ffmpeg_render(
                    ordered_files,
                    durations,
                    spec,
                    output_path,
                    total_duration,
                    progress_cb,
                    music_path,
                )
            except Exception as exc:
                logger.exception("image_to_video render failed job_id=%s user_id=%s", job_id, user_id)
                _safe_fail_job(job_id, user_id, f"Render failed: {exc}")
                return
            logger.info(
                "image_to_video local output ready job_id=%s user_id=%s path=%s exists=%s backend=%s",
                job_id,
                user_id,
                output_path,
                output_path.exists(),
                getattr(storage, "backend_name", "unknown"),
            )
            _safe_update_job(job_id, user_id, status="rendering", progress=90)

            if getattr(storage, "backend_name", "local") == "s3":
                output_key = f"image_to_video/{user_id}/{output_path.name}"
                logger.info(
                    "image_to_video s3 upload begin job_id=%s user_id=%s key=%s path=%s",
                    job_id,
                    user_id,
                    output_key,
                    output_path,
                )
                storage.put_file(output_path, output_key)
                logger.info(
                    "image_to_video s3 upload success job_id=%s user_id=%s key=%s",
                    job_id,
                    user_id,
                    output_key,
                )
                output_url = build_storage_reference(output_key)
            else:
                output_url = f"/video_shorts/media/image_to_video/{output_path.name}"
            logger.info(
                "image_to_video db update begin job_id=%s user_id=%s output_url=%s",
                job_id,
                user_id,
                output_url,
            )
            _update_job(job_id, user_id, status="done", progress=100, output_url=output_url)
            logger.info("image_to_video db update success job_id=%s user_id=%s", job_id, user_id)
    except Exception as exc:
        logger.exception("image_to_video render crashed job_id=%s user_id=%s", job_id, user_id)
        _safe_fail_job(job_id, user_id, f"Render crashed: {exc}")
    finally:
        for temp_path in temp_inputs:
            try:
                temp_path.unlink()
            except Exception:
                pass


def _safe_update_job(job_id: str, user_id: str, status: str, progress: int, output_url: Optional[str] = None) -> None:
    try:
        _update_job(job_id, user_id, status=status, progress=progress, output_url=output_url)
    except Exception:
        pass


def _safe_fail_job(job_id: str, user_id: str, message: str) -> None:
    try:
        _fail_job(job_id, user_id, message)
    except Exception:
        pass


def render_job_from_db(job_id: str) -> None:
    conn = get_db()
    ensure_image_to_video_jobs_schema(conn)
    row = conn.execute(
        "SELECT user_id, payload_json, brand_id FROM image_to_video_jobs WHERE job_id = ?",
        [job_id],
    ).fetchone()
    conn.close()
    if not row:
        return
    user_id, payload_json, brand_id = row
    payload = {}
    if payload_json:
        try:
            payload = json.loads(payload_json)
        except Exception:
            payload = {}
    render_image_to_video_job(job_id, user_id, payload, brand_id=brand_id)


def _run_ffmpeg_render(
    files: List[Path],
    durations: List[float],
    spec: RenderSpec,
    output_path: Path,
    total_duration: float,
    progress_cb=None,
    music_path: Optional[Path] = None,
) -> None:
    ffmpeg = _resolve_ffmpeg()
    debug_transitions = spec.debug_transitions or os.getenv("IMAGE_TO_VIDEO_DEBUG_TRANSITIONS") == "1"
    inputs = []
    filter_parts = []
    labels = []
    uses_watercolor_matte = False
    matte_input_idx_by_transition = {}
    matte_invert_by_transition = {}
    matte_clip_by_transition = {}
    first_watercolor_transition_idx = None

    for idx, (path, duration) in enumerate(zip(files, durations)):
        inputs.extend(["-loop", "1", "-framerate", str(spec.fps), "-t", str(duration), "-i", str(path)])
        frames = max(int(math.ceil(duration * spec.fps)), 1)
        motion_preset = _effective_motion_for_index(spec, idx, len(files))
        motion_filter = _motion_filter(motion_preset, spec.width, spec.height, frames, spec.fps)
        label = f"v{idx}"
        labels.append(label)
        # Normalize timebase/timestamps across all branches so mixed concat/xfade chains remain compatible.
        # zoompan already outputs at target fps, so avoid a second fps stage.
        filter_parts.append(
            f"[{idx}:v]{motion_filter},settb=AVTB,setpts=PTS-STARTPTS[{label}]"
        )

    audio_input_idx = None
    if music_path:
        audio_input_idx = len(files)
        inputs.extend(["-stream_loop", "-1", "-i", str(music_path)])

    for idx in range(max(len(labels) - 1, 0)):
        transition_name, _ = _effective_transition_for_index(spec, idx)
        if transition_name == "watercolor_ink_matte":
            uses_watercolor_matte = True

    if uses_watercolor_matte:
        matte_clips = _list_watercolor_matte_clips()
        if not matte_clips:
            raise RuntimeError(
                f"Watercolor Ink Matte selected but no .mov found in {WATERCOLOR_INK_MATTE_DIR}"
            )
        preferred_clip = _preferred_watercolor_matte_clip(matte_clips)
        if preferred_clip is None:
            raise RuntimeError(
                f"Watercolor Ink Matte selected but no usable .mov found in {WATERCOLOR_INK_MATTE_DIR}"
            )
        for idx in range(max(len(labels) - 1, 0)):
            transition_name, _ = _effective_transition_for_index(spec, idx)
            if transition_name != "watercolor_ink_matte":
                continue
            clip = preferred_clip
            matte_idx = len(files) + (1 if audio_input_idx is not None else 0) + len(matte_input_idx_by_transition)
            matte_input_idx_by_transition[idx] = matte_idx
            clip_name = clip.stem.lower()
            matte_invert_by_transition[idx] = ("reversal" in clip_name) or ("invert" in clip_name)
            matte_clip_by_transition[idx] = clip
            if first_watercolor_transition_idx is None:
                first_watercolor_transition_idx = idx
                if debug_transitions:
                    print(
                        f"[watercolor-debug] clip={clip.name} invert={matte_invert_by_transition[idx]}",
                        flush=True,
                    )
            inputs.extend(["-stream_loop", "-1", "-i", str(clip)])

    filter_chain = ""

    if len(labels) == 1:
        if len(labels) > 1:
            concat_inputs = "".join([f"[{label}]" for label in labels])
            filter_chain = f"{concat_inputs}concat=n={len(labels)}:v=1:a=0[base]"
        else:
            filter_chain = f"[{labels[0]}]format=yuv420p[base]"
        video_duration = durations[0] if durations else total_duration
    else:
        last_label = labels[0]
        current_length = durations[0]
        for idx in range(1, len(labels)):
            next_label = labels[idx]
            xf_label = f"xf{idx}"
            transition_name, trans_duration = _effective_transition_for_index(spec, idx - 1)
            if transition_name == "cut" or trans_duration <= 0:
                filter_parts.append(
                    f"[{last_label}][{next_label}]concat=n=2:v=1:a=0[{xf_label}]"
                )
                current_length += durations[idx]
            elif transition_name == "watercolor_ink_matte":
                matte_input_idx = matte_input_idx_by_transition.get(idx - 1)
                if matte_input_idx is None:
                    raise RuntimeError("Watercolor matte source stream is not available")
                invert_matte = matte_invert_by_transition.get(idx - 1, False)
                effective_duration = max(min(trans_duration, durations[idx], current_length), 0.05)
                a_end = current_length
                a_start = max(a_end - effective_duration, 0.0)
                b_head_end = min(effective_duration, durations[idx])
                matte_start = WATERCOLOR_MATTE_START_SECONDS
                matte_end = matte_start + effective_duration
                progress_steps = max(int(round(effective_duration * spec.fps)) - 1, 1)
                delayed_start_steps = max(int(round(progress_steps * 0.5)), 1)
                ramp_steps = max(progress_steps - delayed_start_steps, 1)

                a_head_label = f"ahead{idx}"
                a_tail_label = f"atail{idx}"
                b_head_label = f"bhead{idx}"
                b_full_label = f"bfull{idx}"
                matte_label = f"wmm{idx}"
                b_rgba_label = f"brgba{idx}"
                b_masked_label = f"bmask{idx}"
                merge_label = f"wover{idx}"

                filter_parts.append(
                    f"[{last_label}]split=2[{a_head_label}_src][{a_tail_label}_src]"
                )
                filter_parts.append(
                    f"[{a_head_label}_src]trim=0:{a_start:.3f},setpts=PTS-STARTPTS[{a_head_label}]"
                )
                filter_parts.append(
                    f"[{a_tail_label}_src]trim={a_start:.3f}:{a_end:.3f},setpts=PTS-STARTPTS[{a_tail_label}]"
                )
                filter_parts.append(
                    f"[{next_label}]split=2[{b_head_label}_src][{b_full_label}]"
                )
                filter_parts.append(
                    f"[{b_head_label}_src]trim=0:{b_head_end:.3f},setpts=PTS-STARTPTS[{b_head_label}]"
                )
                if invert_matte:
                    matte_filter = (
                        f"[{matte_input_idx}:v]fps={spec.fps},scale={spec.width}:{spec.height},format=gray,"
                        f"trim={matte_start:.3f}:{matte_end:.3f},setpts=PTS-STARTPTS,"
                        "lut=y='255-val',eq=contrast=2.0:brightness=-0.05,lut=y='clip((val-36)*1.5,0,255)',"
                        f"geq=lum='clip((lum(X,Y)-(255*(1-pow(clip((N-{delayed_start_steps})/{ramp_steps},0,1),2))))*6,0,255)',"
                        "gblur=sigma=0.8,"
                        f"settb=AVTB[{matte_label}]"
                    )
                else:
                    matte_filter = (
                        f"[{matte_input_idx}:v]fps={spec.fps},scale={spec.width}:{spec.height},format=gray,"
                        f"trim={matte_start:.3f}:{matte_end:.3f},setpts=PTS-STARTPTS,"
                        "eq=contrast=2.0:brightness=-0.05,lut=y='clip((val-36)*1.5,0,255)',"
                        f"geq=lum='clip((lum(X,Y)-(255*(1-pow(clip((N-{delayed_start_steps})/{ramp_steps},0,1),2))))*6,0,255)',"
                        "gblur=sigma=0.8,"
                        f"settb=AVTB[{matte_label}]"
                    )
                filter_parts.append(matte_filter)
                filter_parts.append(
                    f"[{b_head_label}]format=rgba,settb=AVTB,setpts=PTS-STARTPTS[{b_rgba_label}]"
                )
                filter_parts.append(
                    f"[{b_rgba_label}][{matte_label}]alphamerge[{b_masked_label}]"
                )
                filter_parts.append(
                    f"[{a_tail_label}][{b_masked_label}]overlay=shortest=1:eof_action=pass:repeatlast=0:format=auto[{merge_label}]"
                )
                # Keep each image's full slot duration (no overlap subtraction). Transition is an overlay window.
                filter_parts.append(
                    f"[{a_head_label}][{merge_label}][{b_full_label}]concat=n=3:v=1:a=0[{xf_label}]"
                )
                current_length = current_length + durations[idx]
            else:
                offset = max(current_length - trans_duration, 0.0)
                filter_parts.append(
                    f"[{last_label}][{next_label}]xfade=transition={transition_name}:duration={trans_duration}:offset={offset:.2f}[{xf_label}]"
                )
                current_length = current_length + durations[idx] - trans_duration
            last_label = xf_label
        filter_chain = f"[{last_label}]format=yuv420p[base]"
        video_duration = current_length

    style_filter = _style_filter(spec.style_preset)
    overlay_filters = _overlay_filters(spec.overlay, spec.width, spec.height)

    extra = []
    if style_filter:
        extra.append(style_filter)
    extra.extend(overlay_filters)

    if extra:
        filter_chain = f"{filter_chain};[base]{','.join(extra)}[outv]"
        output_label = "[outv]"
    else:
        output_label = "[base]"

    filter_items = filter_parts + [filter_chain]
    audio_output_label = None
    if audio_input_idx is not None:
        audio_output_label = "[aout]"
        filter_items.append(
            f"[{audio_input_idx}:a]atrim={spec.music_start_seconds:.3f}:{spec.music_start_seconds + video_duration:.3f},asetpts=N/SR/TB,volume={spec.music_volume:.3f}{audio_output_label}"
        )

    filter_complex = ";".join(filter_items)

    tmp_handle = tempfile.NamedTemporaryFile(delete=False, suffix=".mp4", dir=str(output_path.parent))
    tmp_path = Path(tmp_handle.name)
    tmp_handle.close()

    cmd = [
        ffmpeg,
        "-y",
        *inputs,
        "-filter_complex",
        filter_complex,
        "-map",
        output_label,
    ]
    if audio_output_label:
        cmd.extend(
            [
                "-map",
                audio_output_label,
                "-c:a",
                "aac",
                "-b:a",
                "192k",
            ]
        )
    cmd.extend(
        [
        "-t",
        f"{video_duration:.3f}",
        "-r",
        str(spec.fps),
        "-preset",
        "superfast",
        "-crf",
        "23",
        "-movflags",
        "+faststart",
        "-pix_fmt",
        "yuv420p",
        str(tmp_path),
        ]
    )

    if FFMPEG_TIMEOUT and FFMPEG_TIMEOUT > 0:
        timeout = FFMPEG_TIMEOUT
    else:
        # Adaptive timeout for heavy slideshow renders with many xfade steps.
        transition_count = max(len(files) - 1, 0)
        complexity = 1.0 + (transition_count * 0.35)
        if music_path:
            complexity += 0.15
        timeout = int(max(900, total_duration * 25.0 * complexity))
    try:
        ffmpeg_log_enabled = os.getenv("IMAGE_TO_VIDEO_LOG_FFMPEG") == "1"
        proc = subprocess.Popen(
            cmd,
            stdout=None if ffmpeg_log_enabled else subprocess.DEVNULL,
            stderr=None if ffmpeg_log_enabled else subprocess.DEVNULL,
        )
        start_time = time.monotonic()
        expected = max(10.0, total_duration * 5.0) if total_duration else 30.0
        while proc.poll() is None:
            elapsed = time.monotonic() - start_time
            ratio = min(elapsed / expected, 1.0)
            if progress_cb:
                progress_cb(25 + ratio * 60)
            if elapsed > timeout:
                proc.kill()
                raise subprocess.TimeoutExpired(cmd, timeout)
            time.sleep(1.0)
        ret = proc.wait(timeout=5)
        if ret != 0:
            raise subprocess.CalledProcessError(ret, cmd)
        tmp_path.replace(output_path)
        if debug_transitions and first_watercolor_transition_idx is not None:
            try:
                _dump_watercolor_debug_artifacts(
                    files=files,
                    durations=durations,
                    spec=spec,
                    transition_idx=first_watercolor_transition_idx,
                    matte_clip=matte_clip_by_transition.get(first_watercolor_transition_idx),
                    invert_matte=matte_invert_by_transition.get(first_watercolor_transition_idx, False),
                    output_path=output_path,
                )
            except Exception as debug_exc:
                print(f"[watercolor-debug] artifact dump failed: {debug_exc}", flush=True)
    except Exception:
        try:
            if tmp_path.exists():
                tmp_path.unlink()
        except Exception:
            pass
        raise


def _dump_watercolor_debug_artifacts(
    files: List[Path],
    durations: List[float],
    spec: RenderSpec,
    transition_idx: int,
    matte_clip: Optional[Path],
    invert_matte: bool,
    output_path: Path,
) -> None:
    if matte_clip is None:
        return
    if transition_idx < 0 or transition_idx + 1 >= len(files):
        return

    ffmpeg = _resolve_ffmpeg()
    a_file = files[transition_idx]
    b_file = files[transition_idx + 1]
    a_duration = durations[transition_idx]
    b_duration = durations[transition_idx + 1]
    effective_duration = max(min(WATERCOLOR_MATTE_TRANSITION_SECONDS, a_duration, b_duration), 0.05)
    a_start = max(a_duration - effective_duration, 0.0)
    b_start = min(effective_duration, b_duration)
    matte_start = WATERCOLOR_MATTE_START_SECONDS
    matte_end = matte_start + effective_duration
    threshold_steps = max(int(round(effective_duration * spec.fps)) - 1, 1)

    a_motion = _motion_filter(_effective_motion_for_index(spec, transition_idx, len(files)), spec.width, spec.height, max(int(math.ceil(a_duration * spec.fps)), 1), spec.fps)
    b_motion = _motion_filter(_effective_motion_for_index(spec, transition_idx + 1, len(files)), spec.width, spec.height, max(int(math.ceil(b_duration * spec.fps)), 1), spec.fps)

    debug_dir = output_path.parent / f"{output_path.stem}_watercolor_debug"
    debug_dir.mkdir(parents=True, exist_ok=True)
    a_tail_path = debug_dir / "a_tail.mp4"
    matte_path = debug_dir / "matte.mp4"
    b_masked_path = debug_dir / "b_masked.mp4"
    merge_path = debug_dir / "merge.mp4"

    matte_ops = "lut=y='255-val'," if invert_matte else ""
    filter_complex = (
        f"[0:v]{a_motion},settb=AVTB,setpts=PTS-STARTPTS[va];"
        f"[1:v]{b_motion},settb=AVTB,setpts=PTS-STARTPTS[vb];"
        f"[va]trim={a_start:.3f}:{a_duration:.3f},setpts=PTS-STARTPTS[a_tail];"
        f"[vb]trim=0:{b_start:.3f},setpts=PTS-STARTPTS[b_head];"
        f"[2:v]fps={spec.fps},scale={spec.width}:{spec.height},format=gray,"
        f"trim={matte_start:.3f}:{matte_end:.3f},setpts=PTS-STARTPTS,"
        f"{matte_ops}eq=contrast=2.0:brightness=-0.05,lut=y='clip((val-36)*1.5,0,255)',"
        f"geq=lum='clip((lum(X,Y)-(255*(1-pow(N/{threshold_steps},2))))*6,0,255)',gblur=sigma=0.8,settb=AVTB[matte_src];"
        f"[matte_src]split=2[matte_out][matte_merge];"
        f"[b_head]format=rgba,settb=AVTB,setpts=PTS-STARTPTS[brgba];"
        f"[brgba][matte_merge]alphamerge[bm_src];"
        f"[a_tail]split=2[a_tail_out][a_tail_merge];"
        f"[bm_src]split=2[bm_out][bm_merge];"
        f"[a_tail_merge][bm_merge]overlay=shortest=1:eof_action=pass:repeatlast=0:format=auto[merge]"
    )

    cmd = [
        ffmpeg,
        "-y",
        "-loop",
        "1",
        "-framerate",
        str(spec.fps),
        "-t",
        str(a_duration),
        "-i",
        str(a_file),
        "-loop",
        "1",
        "-framerate",
        str(spec.fps),
        "-t",
        str(b_duration),
        "-i",
        str(b_file),
        "-i",
        str(matte_clip),
        "-filter_complex",
        filter_complex,
        "-map",
        "[a_tail_out]",
        "-an",
        "-c:v",
        "libx264",
        "-pix_fmt",
        "yuv420p",
        str(a_tail_path),
        "-map",
        "[matte_out]",
        "-an",
        "-c:v",
        "libx264",
        "-pix_fmt",
        "yuv420p",
        str(matte_path),
        "-map",
        "[bm_out]",
        "-an",
        "-c:v",
        "libx264",
        "-pix_fmt",
        "yuv420p",
        str(b_masked_path),
        "-map",
        "[merge]",
        "-an",
        "-c:v",
        "libx264",
        "-pix_fmt",
        "yuv420p",
        str(merge_path),
    ]
    subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def _update_job(job_id: str, user_id: str, status: str, progress: int, output_url: Optional[str] = None) -> None:
    conn = get_db()
    ensure_image_to_video_jobs_schema(conn)
    try:
        conn.execute(
            """
            UPDATE image_to_video_jobs
            SET status = ?, progress = ?, output_url = coalesce(?, output_url), error_message = NULL, updated_at = now()
            WHERE job_id = ? AND user_id = ?
            """,
            [status, progress, output_url, job_id, user_id],
        )
        conn.commit()
    finally:
        conn.close()


def _fail_job(job_id: str, user_id: str, message: str) -> None:
    conn = get_db()
    ensure_image_to_video_jobs_schema(conn)
    try:
        conn.execute(
            """
            UPDATE image_to_video_jobs
            SET status = ?, error_message = ?, updated_at = now()
            WHERE job_id = ? AND user_id = ?
            """,
            ["failed", message, job_id, user_id],
        )
        conn.commit()
    finally:
        conn.close()


def _parse_args(argv: List[str]) -> Optional[str]:
    parser = argparse.ArgumentParser()
    parser.add_argument("--job-id", dest="job_id", required=True)
    args = parser.parse_args(argv)
    return args.job_id


if __name__ == "__main__":
    job_id = _parse_args(sys.argv[1:])
    if job_id:
        render_job_from_db(job_id)
