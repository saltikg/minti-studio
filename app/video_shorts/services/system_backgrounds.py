import hashlib
from pathlib import Path
from typing import List, Optional

from app.video_shorts.config import MINTI_BACKGROUNDS_DIR


SYSTEM_BACKGROUND_KEY_PREFIX = "systembg:"
_ALLOWED_SYSTEM_BACKGROUND_EXTS = {".png", ".jpg", ".jpeg", ".webp"}


def list_system_background_paths() -> List[Path]:
    if not MINTI_BACKGROUNDS_DIR.exists():
        return []
    return sorted(
        [
            entry
            for entry in MINTI_BACKGROUNDS_DIR.iterdir()
            if entry.is_file() and entry.suffix.lower() in _ALLOWED_SYSTEM_BACKGROUND_EXTS
        ],
        key=lambda entry: entry.name.lower(),
    )


def make_system_background_key(filename: str) -> str:
    return f"{SYSTEM_BACKGROUND_KEY_PREFIX}{Path(filename).name}"


def is_system_background_key(value: Optional[str]) -> bool:
    return str(value or "").startswith(SYSTEM_BACKGROUND_KEY_PREFIX)


def system_background_filename_from_key(value: Optional[str]) -> str:
    if not is_system_background_key(value):
        return ""
    return Path(str(value).split(":", 1)[1]).name


def resolve_system_background_path(value: Optional[str]) -> Optional[Path]:
    target_name = system_background_filename_from_key(value) or Path(str(value or "")).name
    if not target_name:
        return None
    for candidate in list_system_background_paths():
        if candidate.name == target_name:
            return candidate
    return None


def choose_deterministic_system_background(video_id: Optional[str]) -> Optional[Path]:
    candidates = [candidate for candidate in list_system_background_paths() if candidate.exists()]
    if not candidates:
        return None
    if not video_id:
        return candidates[0]
    digest = hashlib.sha256(str(video_id).encode("utf-8")).digest()
    index = int.from_bytes(digest[:8], "big") % len(candidates)
    return candidates[index]
