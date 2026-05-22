import json
from typing import Any, Dict

from app.video_shorts.config import SHORTS_DIR


def _overrides_path(video_id: str):
    return SHORTS_DIR / f"{video_id}_non_speech_overrides.json"


def load_non_speech_overrides(video_id: str) -> Dict[str, Any]:
    if not video_id:
        return {}
    path = _overrides_path(video_id)
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text())
    except Exception:
        return {}
    if not isinstance(data, dict):
        return {}
    return data


def save_non_speech_overrides(video_id: str, overrides: Dict[str, Any]) -> None:
    if not video_id:
        return
    path = _overrides_path(video_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(overrides or {}, ensure_ascii=False, indent=2))
