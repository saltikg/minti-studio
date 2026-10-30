from dataclasses import dataclass
import logging
from typing import Optional

from app.video_shorts import config
from app.video_shorts.services.billing import plan_is_paid
from app.video_shorts.services.db import get_db_readonly


EXPORT_RESOLUTION_OVERRIDE_KEY = "export_resolution_override"
logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ExportResolutionDecision:
    resolution: str
    override: Optional[str]
    eligible: bool
    flag_enabled: bool
    is_guest: bool


def resolve_export_resolution(user_id: Optional[str], *, conn=None) -> ExportResolutionDecision:
    """Resolve the account-level portrait export resolution, defaulting safely to 720p."""
    normalized_user_id = str(user_id or "").strip()
    if not normalized_user_id:
        return ExportResolutionDecision("720", None, False, bool(config.EXPORT_1080_FOR_PAID), True)

    own_conn = conn is None
    db = conn
    try:
        if db is None:
            db = get_db_readonly()
        account_row = db.execute(
            """
            SELECT
                COALESCE(plan_id, ''),
                lower(COALESCE(service_mode, '')),
                COALESCE(is_guest, FALSE)
            FROM shorts_users
            WHERE CAST(id AS VARCHAR) = CAST(? AS VARCHAR)
            LIMIT 1
            """,
            [normalized_user_id],
        ).fetchone()
        if not account_row:
            return ExportResolutionDecision("720", None, False, bool(config.EXPORT_1080_FOR_PAID), True)

        plan_id = str(account_row[0] or "").strip()
        service_mode = str(account_row[1] or "").strip().lower()
        is_guest = bool(account_row[2])
        eligible = bool(plan_is_paid(plan_id) or service_mode == "autopilot")
        flag_enabled = bool(config.EXPORT_1080_FOR_PAID)

        if is_guest:
            return ExportResolutionDecision("720", None, False, flag_enabled, True)

        override_row = db.execute(
            """
            SELECT preference_value
            FROM shorts_user_preferences
            WHERE CAST(user_id AS VARCHAR) = CAST(? AS VARCHAR)
              AND preference_key = ?
            LIMIT 1
            """,
            [normalized_user_id, EXPORT_RESOLUTION_OVERRIDE_KEY],
        ).fetchone()
        override = str((override_row[0] if override_row else "") or "").strip()
        if override not in {"720", "1080"}:
            override = None

        if override:
            resolution = override
        elif eligible and flag_enabled:
            resolution = "1080"
        else:
            resolution = "720"
        return ExportResolutionDecision(resolution, override, eligible, flag_enabled, False)
    except Exception as exc:
        logger.warning("Export resolution lookup failed for user %s: %s", normalized_user_id, exc)
        return ExportResolutionDecision("720", None, False, bool(config.EXPORT_1080_FOR_PAID), False)
    finally:
        if own_conn and db is not None:
            db.close()
