#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import logging
import shutil
import sys
from pathlib import Path

from dotenv import load_dotenv


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
load_dotenv(ROOT / ".env")

from app.video_shorts.services.email_verification import send_resend_email


logger = logging.getLogger(__name__)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Send an email alarm when disk usage exceeds a threshold.")
    parser.add_argument("--path", default="/", help="Filesystem path to check. Default: /")
    parser.add_argument("--threshold", type=int, default=80, help="Usage percentage threshold. Default: 80")
    parser.add_argument("--recipient", default="info@mintistudio.com", help="Alarm email recipient.")
    parser.add_argument(
        "--state-file",
        default=str(ROOT / "data" / "disk_usage_alarm_state.json"),
        help="Path to the persisted alert state file.",
    )
    return parser.parse_args()


def _usage_percent(path: str) -> int:
    usage = shutil.disk_usage(path)
    if usage.total <= 0:
        return 0
    return int(round((usage.used / usage.total) * 100))


def _load_state(state_path: Path) -> dict:
    try:
        payload = json.loads(state_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except Exception:
        logger.warning("disk usage alarm state unreadable path=%s; treating as empty state", state_path)
        return {}
    return payload if isinstance(payload, dict) else {}


def _save_state(state_path: Path, payload: dict) -> None:
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(json.dumps(payload, ensure_ascii=True, indent=2, sort_keys=True), encoding="utf-8")


def _send_high_usage_email(*, recipient: str, path: str, threshold: int, used_pct: int) -> None:
    subject = f"[MintiStudio] Disk usage {used_pct}% on {path}"
    html = (
        "<div style=\"font-family:Arial,sans-serif;line-height:1.6;color:#0f172a;max-width:640px;margin:0 auto;\">"
        "<div style=\"padding:24px;border:1px solid #dbe4f0;border-radius:16px;background:#ffffff;\">"
        f"<div style=\"font-size:24px;font-weight:700;margin-bottom:16px;\">Disk usage alert</div>"
        f"<p style=\"margin:0 0 10px;\"><strong>Path:</strong> {path}</p>"
        f"<p style=\"margin:0 0 10px;\"><strong>Usage:</strong> {used_pct}%</p>"
        f"<p style=\"margin:0;\"><strong>Threshold:</strong> {threshold}%</p>"
        "</div></div>"
    )
    text = (
        "Disk usage alert\n\n"
        f"Path: {path}\n"
        f"Usage: {used_pct}%\n"
        f"Threshold: {threshold}%\n"
    )
    send_resend_email(
        to_email=recipient,
        subject=subject,
        html=html,
        text=text,
        error_message="Disk alert email could not be sent.",
    )


def _send_recovery_email(*, recipient: str, path: str, threshold: int, used_pct: int) -> None:
    subject = f"[MintiStudio] Disk usage recovered to {used_pct}% on {path}"
    html = (
        "<div style=\"font-family:Arial,sans-serif;line-height:1.6;color:#0f172a;max-width:640px;margin:0 auto;\">"
        "<div style=\"padding:24px;border:1px solid #dbe4f0;border-radius:16px;background:#ffffff;\">"
        f"<div style=\"font-size:24px;font-weight:700;margin-bottom:16px;\">Disk usage recovered</div>"
        f"<p style=\"margin:0 0 10px;\"><strong>Path:</strong> {path}</p>"
        f"<p style=\"margin:0 0 10px;\"><strong>Usage:</strong> {used_pct}%</p>"
        f"<p style=\"margin:0;\"><strong>Threshold:</strong> {threshold}%</p>"
        "</div></div>"
    )
    text = (
        "Disk usage recovered\n\n"
        f"Path: {path}\n"
        f"Usage: {used_pct}%\n"
        f"Threshold: {threshold}%\n"
    )
    send_resend_email(
        to_email=recipient,
        subject=subject,
        html=html,
        text=text,
        error_message="Disk recovery email could not be sent.",
    )


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    args = _parse_args()
    state_path = Path(args.state_file).expanduser()
    state = _load_state(state_path)
    used_pct = _usage_percent(args.path)
    threshold = int(args.threshold)
    is_above = used_pct >= threshold
    was_above = bool(state.get("above_threshold"))

    if not is_above:
        if was_above:
            _send_recovery_email(
                recipient=args.recipient,
                path=args.path,
                threshold=threshold,
                used_pct=used_pct,
            )
            print(f"disk usage recovery sent: {used_pct}% < {threshold}% on {args.path} to {args.recipient}")
        else:
            print(f"disk usage ok: {used_pct}% < {threshold}% on {args.path}")
        _save_state(
            state_path,
            {
                "above_threshold": False,
                "last_path": args.path,
                "last_threshold": threshold,
                "last_used_pct": used_pct,
            },
        )
        return 0

    logger.warning(
        "disk usage above threshold path=%s usage_pct=%s threshold_pct=%s",
        args.path,
        used_pct,
        threshold,
    )
    if not was_above:
        _send_high_usage_email(
            recipient=args.recipient,
            path=args.path,
            threshold=threshold,
            used_pct=used_pct,
        )
        print(f"disk usage alert sent: {used_pct}% >= {threshold}% on {args.path} to {args.recipient}")
    else:
        print(f"disk usage still high: {used_pct}% >= {threshold}% on {args.path}; email suppressed")
    _save_state(
        state_path,
        {
            "above_threshold": True,
            "last_path": args.path,
            "last_threshold": threshold,
            "last_used_pct": used_pct,
        },
    )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(str(exc), file=sys.stderr)
        raise
