#!/usr/bin/env python3
from __future__ import annotations

import sys
from pathlib import Path


ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from app.video_shorts.services.instagram_api import (  # noqa: E402
    InstagramActionError,
    subscribe_instagram_comment_webhooks,
)
from src.trends.instagram_tokens import list_instagram_credentials  # noqa: E402


def main() -> int:
    credentials = list_instagram_credentials()
    if not credentials:
        print("No connected Instagram accounts found.")
        return 0

    success_count = 0
    failure_count = 0
    for creds in credentials:
        scoped_user_id = str(creds.get("user_id") or "").strip()
        ig_user_id = str(creds.get("instagram_business_account_id") or "").strip()
        token = str(creds.get("page_access_token") or "").strip()
        username = str(creds.get("instagram_username") or "").strip() or "-"
        if not ig_user_id or not token:
            failure_count += 1
            print(
                f"skip user_id={scoped_user_id or '-'} username={username} reason=missing_credentials"
            )
            continue
        try:
            payload = subscribe_instagram_comment_webhooks(ig_user_id, token)
            success_count += 1
            print(
                f"ok user_id={scoped_user_id or '-'} ig_user_id={ig_user_id} "
                f"username={username} success={payload.get('success')}"
            )
        except InstagramActionError as exc:
            failure_count += 1
            print(
                f"fail user_id={scoped_user_id or '-'} ig_user_id={ig_user_id} "
                f"username={username} error={exc}"
            )

    print(
        f"done total={len(credentials)} success={success_count} failure={failure_count}"
    )
    return 0 if failure_count == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
