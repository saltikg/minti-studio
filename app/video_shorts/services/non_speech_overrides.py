from typing import Any, Dict
from app.video_shorts.services import tenant_media


def load_non_speech_overrides(video_id: str, *, video_pk=None) -> Dict[str, Any]:
    return tenant_media.load_state(video_id, "non_speech_overrides", video_pk=video_pk)


def save_non_speech_overrides(video_id: str, overrides: Dict[str, Any], *, video_pk=None) -> None:
    tenant_media.write_state(video_id, overrides or {}, "non_speech_overrides", video_pk=video_pk)
