#!/usr/bin/env python3
from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

from dotenv import load_dotenv


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
load_dotenv(ROOT / ".env")

from app.video_shorts.services.email_verification import send_resend_email


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Send an email alarm when disk usage exceeds a threshold.")
    parser.add_argument("--path", default="/", help="Filesystem path to check. Default: /")
    parser.add_argument("--threshold", type=int, default=80, help="Usage percentage threshold. Default: 80")
    parser.add_argument("--recipient", default="info@mintistudio.com", help="Alarm email recipient.")
    return parser.parse_args()


def _usage_percent(path: str) -> int:
    usage = shutil.disk_usage(path)
    if usage.total <= 0:
        return 0
    return int(round((usage.used / usage.total) * 100))


def main() -> int:
    args = _parse_args()
    used_pct = _usage_percent(args.path)
    if used_pct < int(args.threshold):
        print(f"disk usage ok: {used_pct}% < {int(args.threshold)}% on {args.path}")
        return 0

    subject = f"[MintiStudio] Disk usage {used_pct}% on {args.path}"
    html = (
        "<div style=\"font-family:Arial,sans-serif;line-height:1.6;color:#0f172a;max-width:640px;margin:0 auto;\">"
        "<div style=\"padding:24px;border:1px solid #dbe4f0;border-radius:16px;background:#ffffff;\">"
        f"<div style=\"font-size:24px;font-weight:700;margin-bottom:16px;\">Disk usage alert</div>"
        f"<p style=\"margin:0 0 10px;\"><strong>Path:</strong> {args.path}</p>"
        f"<p style=\"margin:0 0 10px;\"><strong>Usage:</strong> {used_pct}%</p>"
        f"<p style=\"margin:0;\"><strong>Threshold:</strong> {int(args.threshold)}%</p>"
        "</div></div>"
    )
    text = (
        "Disk usage alert\n\n"
        f"Path: {args.path}\n"
        f"Usage: {used_pct}%\n"
        f"Threshold: {int(args.threshold)}%\n"
    )
    send_resend_email(
        to_email=args.recipient,
        subject=subject,
        html=html,
        text=text,
        error_message="Disk alert email could not be sent.",
    )
    print(f"disk usage alert sent: {used_pct}% >= {int(args.threshold)}% on {args.path} to {args.recipient}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(str(exc), file=sys.stderr)
        raise
