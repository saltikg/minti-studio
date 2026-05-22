from __future__ import annotations

from typing import Any, Dict, List, Optional

import requests

from app.video_shorts.config import HEYGEN_API_BASE, HEYGEN_API_KEY


class HeyGenError(RuntimeError):
    pass


class HeyGenClient:
    def __init__(self, api_key: Optional[str] = None, api_base: Optional[str] = None):
        self.api_key = (api_key or HEYGEN_API_KEY or "").strip()
        self.api_base = (api_base or HEYGEN_API_BASE or "https://api.heygen.com").rstrip("/")

    @property
    def configured(self) -> bool:
        return bool(self.api_key)

    def _request(self, method: str, path: str, *, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        if not self.configured:
            raise HeyGenError("HEYGEN_API_KEY is not configured.")
        url = f"{self.api_base}{path}"
        try:
            resp = requests.request(
                method,
                url,
                headers={
                    "Accept": "application/json",
                    "X-Api-Key": self.api_key,
                },
                params=params,
                timeout=20,
            )
            resp.raise_for_status()
        except requests.RequestException as exc:
            raise HeyGenError(f"HeyGen request failed: {exc}") from exc
        try:
            return resp.json() or {}
        except ValueError as exc:
            raise HeyGenError("HeyGen returned a non-JSON response.") from exc

    def list_avatars(self) -> List[Dict[str, Any]]:
        payload = self._request("GET", "/v2/avatars")
        avatars = (((payload.get("data") or {}).get("avatars")) or [])
        normalized: List[Dict[str, Any]] = []
        for avatar in avatars:
            avatar_id = str(avatar.get("avatar_id") or "").strip()
            if not avatar_id:
                continue
            normalized.append(
                {
                    "avatar_id": avatar_id,
                    "avatar_name": str(avatar.get("avatar_name") or "").strip() or avatar_id,
                    "gender": str(avatar.get("gender") or "").strip(),
                    "preview_image_url": str(avatar.get("preview_image_url") or "").strip(),
                    "preview_video_url": str(avatar.get("preview_video_url") or "").strip(),
                    "source": "heygen",
                }
            )
        normalized.sort(key=lambda item: item["avatar_name"].lower())
        return normalized


def get_heygen_background_capabilities() -> Dict[str, Any]:
    return {
        "provider": "heygen",
        "has_background_listing_api": False,
        "supported_background_types": ["color", "image", "video"],
        "notes": (
            "HeyGen background selection is handled at video generation time. "
            "The API supports color, image, and video backgrounds, typically via URL or uploaded asset ID."
        ),
        "docs_url": "https://docs.heygen.com/docs/customize-video-background",
    }
