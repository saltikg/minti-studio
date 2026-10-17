from __future__ import annotations

import threading

from app.video_shorts.config import (
    VIDEO_SHORTS_DATABASE_URL,
    VIDEO_SHORTS_DB,
    VIDEO_SHORTS_DB_BACKEND,
)
from app.video_shorts.services.brands import ensure_brand_schema
from app.video_shorts.services.db import (
    _ensure_video_crop_schema,
    ensure_auth_user_schema,
    ensure_channel_owner_schema,
    ensure_storage_user_schema,
    ensure_user_events_schema,
    get_db,
)
from app.video_shorts.services.usage_metering import ensure_usage_metering_schema
from app.video_shorts.services.ai_video_workspace import ensure_ai_video_schema
from app.video_shorts.services.subscriber_metrics import ensure_subscriber_snapshot_table
from app.video_shorts.services.video_metrics import ensure_snapshot_table
from app.video_shorts.services.db import (
    ensure_background_preferences_schema,
    ensure_user_preferences_schema,
)


_schema_lock = threading.Lock()
_schema_ready = False


def initialize_runtime_schema() -> None:
    """Run request prerequisites once per process, before serving traffic."""
    global _schema_ready
    if VIDEO_SHORTS_DB_BACKEND == "postgres":
        if not VIDEO_SHORTS_DATABASE_URL:
            return
    elif not VIDEO_SHORTS_DB:
        return
    if _schema_ready:
        return
    with _schema_lock:
        if _schema_ready:
            return
        conn = get_db()
        try:
            ensure_auth_user_schema(conn)
            ensure_storage_user_schema(conn)
            ensure_user_events_schema(conn)
            ensure_brand_schema(conn)
            ensure_channel_owner_schema(conn)
            _ensure_video_crop_schema(conn)
            ensure_usage_metering_schema(conn)
            ensure_snapshot_table(conn)
            ensure_subscriber_snapshot_table(conn)
            ensure_user_preferences_schema(conn)
            ensure_background_preferences_schema(conn)
            ensure_ai_video_schema(conn)
            conn.commit()
            _schema_ready = True
        finally:
            conn.close()
