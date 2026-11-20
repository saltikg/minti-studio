#!/usr/bin/env python3
"""Replace retired blog UI screenshots with active manifest v2 visuals."""

from __future__ import annotations

import argparse
import json
import re
import shutil
import sys
from pathlib import Path
from typing import Any
from urllib.parse import quote, unquote

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.safe_to_deploy import _load_env_file  # noqa: E402

_load_env_file()

from app.video_shorts.services.blog_images import _add_logo_badge  # noqa: E402
from app.video_shorts.services.db import get_db  # noqa: E402
from scripts import blog_pipeline  # noqa: E402

PROTECTED_SLUGS = {"getting-started-with-mintistudio", "free-youtube-to-shorts-tool"}
DOCTORS_SLUG = "youtube-shorts-for-doctors-guide"
DOCTORS_OLD_SLUG = "youtube-shorts-for-doctors-practical-guide-educational-clips-without-extra-admin"
IMAGE_RE = re.compile(
    r'!\[(?P<alt>[^\]\n]*)\]\((?P<url>/video_shorts/static/img/blog/[^)\s"]+)'
    r'(?:\s+"(?P<title>[^"]*)")?\)'
)

LEGACY_BASENAME_TO_ACTIVE_ID: dict[str, str | None] = {
    "1-minti-dashboard.png": None,
    "2-minti-barchart.png": None,
    "3-minti-youtube-video-url-paste.png": None,
    "4-manual-changing-video-details.png": "clip-edit-modal",
    "5-manual-video-creation.png": "transcript-selection",
    "6-my-videos-page.png": "transcript-clips-overview",
    "7-scheduled-videos-calender.png": "schedule-publish-modal",
    "8-manual-video-scheduling.png": "schedule-publish-modal",
    "9-generate-video-screen.png": "transcript-clips-overview",
    "10-manual-video-crop.png": "crop-style-templates",
    "11-ai-suggested-video-schedule-all-function.png": "clips-suggest-ai",
    "minti_power_editor.png": "transcript-clips-overview",
    "minti_practical_scheduling.png": "schedule-publish-modal",
}

GETTING_STARTED_TO_ACTIVE_ID: dict[str, str | None] = {
    "01-open-account.png": None,
    "02-my-videos.png": "transcript-clips-overview",
    "03-start-analysis.png": "transcript-clips-overview",
    "04-edit-transcript.png": "transcript-selection",
    "05-suggest-clips.png": "clips-suggest-ai",
    "06-crop-style.png": "crop-style-templates",
    "07-create-clip.png": "clip-edit-modal",
    "08-render.png": None,
    "09-schedule-publish.png": "schedule-publish-modal",
    "10-track.png": None,
}


def _active_manifest() -> dict[str, dict[str, Any]]:
    return {str(item["id"]): item for item in blog_pipeline._load_manifest()}


def _source_mapping(slug: str, url: str) -> tuple[bool, str | None]:
    basename = Path(unquote(url)).name
    if slug == DOCTORS_SLUG and basename == "3-minti-youtube-video-url-paste.png":
        return True, "real-short-doctor-busy"
    if "/getting-started/" in url:
        return True, GETTING_STARTED_TO_ACTIVE_ID.get(basename)
    if basename in LEGACY_BASENAME_TO_ACTIVE_ID:
        return True, LEGACY_BASENAME_TO_ACTIVE_ID[basename]
    return False, None


def _copy_image(entry: dict[str, Any], slug: str, *, apply: bool) -> str:
    source = blog_pipeline._manifest_source_path(str(entry["filename"]))
    target_dir = blog_pipeline.STATIC_BLOG_ROOT / slug
    target = target_dir / source.name
    if apply:
        target_dir.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
        _add_logo_badge(target, badge_ratio=0.14, margin_ratio=0.02)
    return f"/video_shorts/static/img/blog/{quote(slug)}/{quote(source.name)}"


def _replacement_markup(entry: dict[str, Any], slug: str, caption: str, *, apply: bool) -> str:
    media_type = str(entry.get("type") or "screenshot")
    clean_caption = re.sub(r"\s*\[(?:screenshot|graphic)\]\s*$", "", caption or "", flags=re.I).strip()
    clean_caption = clean_caption or str(entry.get("alt") or "Minti Studio visual")
    if media_type == "video":
        return f":::motion {entry['id']}\n{clean_caption}\n:::"
    url = _copy_image(entry, slug, apply=apply)
    alt = str(entry.get("alt") or clean_caption).replace("]", "")
    title = clean_caption.replace('"', "'")
    return f'![{alt}]({url} "{title} [{media_type}]")'


def _rewrite_article(
    slug: str,
    content: str,
    active: dict[str, dict[str, Any]],
    *,
    apply: bool,
) -> tuple[str, list[dict[str, str]], list[dict[str, str]]]:
    changes: list[dict[str, str]] = []
    lost: list[dict[str, str]] = []
    used_ids: set[str] = set(re.findall(r"(?m)^:::motion\s+([a-z0-9-]+)\s*$", content or ""))
    library_count = len(used_ids)
    video_count = sum(1 for item_id in used_ids if str((active.get(item_id) or {}).get("type") or "") == "video")

    def replace(match: re.Match[str]) -> str:
        nonlocal library_count, video_count
        url = str(match.group("url") or "")
        mapped, active_id = _source_mapping(slug, url)
        if not mapped:
            return match.group(0)
        basename = Path(unquote(url)).name
        if not active_id:
            lost.append({"file": basename, "reason": "no clear active replacement"})
            return ""
        entry = active.get(active_id)
        if not entry:
            raise RuntimeError(f"mapped manifest id is not active: {active_id}")
        is_video = str(entry.get("type") or "") == "video"
        if active_id in used_ids:
            lost.append({"file": basename, "reason": f"duplicate replacement {active_id}"})
            return ""
        if library_count >= 2 or (is_video and video_count >= 1):
            lost.append({"file": basename, "reason": "library visual limit"})
            return ""
        caption = str(match.group("title") or match.group("alt") or "")
        replacement = _replacement_markup(entry, slug, caption, apply=apply)
        changes.append({"from": url, "to": active_id})
        used_ids.add(active_id)
        library_count += 1
        video_count += int(is_video)
        return replacement

    rewritten = IMAGE_RE.sub(replace, content or "")
    rewritten = re.sub(r"\n{3,}", "\n\n", rewritten).strip()
    if slug == DOCTORS_SLUG:
        rewritten, count = re.subn(
            r"Most doctors do not stop because Shorts are ineffective\.\s+They stop because the workflow is annoying\.",
            "Many doctors stop not because Shorts don't work, but because the workflow is annoying.",
            rewritten,
            count=1,
        )
        if count != 1 and "Many doctors stop not because Shorts don't work" not in rewritten:
            raise RuntimeError("doctors wording target was not found")
    return rewritten, changes, lost


def _protected_old_images(rows: list[tuple[Any, ...]]) -> dict[str, list[str]]:
    report: dict[str, list[str]] = {}
    for _article_id, slug, content, _cover_image_url in rows:
        if str(slug) not in PROTECTED_SLUGS:
            continue
        report[str(slug)] = [str(match.group("url")) for match in IMAGE_RE.finditer(str(content or ""))]
    return report


def _migrate_doctors_cover(slug: str, cover_image_url: str, *, apply: bool) -> tuple[str, dict[str, str] | None]:
    old_prefix = f"/video_shorts/static/img/blog/{DOCTORS_OLD_SLUG}/"
    if slug != DOCTORS_SLUG or not str(cover_image_url or "").startswith(old_prefix):
        return cover_image_url, None
    filename = Path(unquote(str(cover_image_url))).name
    new_url = f"/video_shorts/static/img/blog/{quote(DOCTORS_SLUG)}/{quote(filename)}"
    if apply:
        source = blog_pipeline.STATIC_BLOG_ROOT / DOCTORS_OLD_SLUG / filename
        target_dir = blog_pipeline.STATIC_BLOG_ROOT / DOCTORS_SLUG
        if not source.is_file():
            raise RuntimeError(f"doctors cover source is missing: {source}")
        target_dir.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target_dir / filename)
    return new_url, {"from": cover_image_url, "to": new_url}


def run(*, apply: bool) -> dict[str, Any]:
    active = _active_manifest()
    conn = get_db()
    try:
        rows = conn.execute(
            "SELECT id, slug, content, cover_image_url FROM blog_articles WHERE status = 'published' ORDER BY slug"
        ).fetchall()
        report: dict[str, Any] = {
            "mode": "apply" if apply else "dry-run",
            "changed_posts": [],
            "lost_images": [],
            "protected_old_images": _protected_old_images(rows),
        }
        for article_id, slug_value, content_value, cover_value in rows:
            slug = str(slug_value or "")
            if slug in PROTECTED_SLUGS:
                continue
            rewritten, changes, lost = _rewrite_article(
                slug,
                str(content_value or ""),
                active,
                apply=apply,
            )
            new_cover, cover_change = _migrate_doctors_cover(slug, str(cover_value or ""), apply=apply)
            if changes or lost or cover_change or rewritten != str(content_value or ""):
                report["changed_posts"].append({"slug": slug, "replacements": changes, "removed": lost, "cover": cover_change})
                report["lost_images"].extend({"slug": slug, **item} for item in lost)
                if apply:
                    conn.execute(
                        "UPDATE blog_articles SET content = ?, cover_image_url = ?, content_updated_at = CURRENT_TIMESTAMP, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                        [rewritten, new_cover, int(article_id)],
                    )
        if apply:
            conn.commit()
        else:
            conn.rollback()
        return report
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def cleanup_orphans() -> dict[str, Any]:
    conn = get_db()
    try:
        rows = conn.execute("SELECT content FROM blog_articles WHERE status = 'published'").fetchall()
    finally:
        conn.close()
    referenced = "\n".join(str(row[0] or "") for row in rows)
    candidates = set(LEGACY_BASENAME_TO_ACTIVE_ID) | set(GETTING_STARTED_TO_ACTIVE_ID)
    removed: list[str] = []
    for path in blog_pipeline.STATIC_BLOG_ROOT.rglob("*"):
        if not path.is_file() or path.name not in candidates:
            continue
        relative = path.relative_to(blog_pipeline.STATIC_BLOG_ROOT)
        if relative.parts[0] in {"library", "getting-started", "free-youtube-to-shorts-tool"}:
            continue
        public_url = "/video_shorts/static/img/blog/" + "/".join(quote(part) for part in relative.parts)
        if public_url in referenced:
            continue
        path.unlink()
        removed.append(str(relative))
    return {"removed_orphans": sorted(removed)}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--cleanup-orphans", action="store_true")
    args = parser.parse_args()
    result = cleanup_orphans() if args.cleanup_orphans else run(apply=args.apply)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
