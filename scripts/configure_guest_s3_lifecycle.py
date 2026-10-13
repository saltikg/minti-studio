#!/usr/bin/env python3
from __future__ import annotations

import json
import sys
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

load_dotenv(ROOT / ".env")

from app.video_shorts.services.storage import GUEST_S3_TAG_KEY, GUEST_S3_TAG_VALUE, get_media_storage


RULE_ID = "minti-guest-expire-4-days"


def _guest_rule() -> dict:
    return {
        "ID": RULE_ID,
        "Status": "Enabled",
        "Filter": {"Tag": {"Key": GUEST_S3_TAG_KEY, "Value": GUEST_S3_TAG_VALUE}},
        "Expiration": {"Days": 4},
    }


def main() -> int:
    storage = get_media_storage()
    client = getattr(storage, "client", None)
    bucket = getattr(storage, "bucket_name", "")
    if client is None or not bucket:
        print("lifecycle_status=skipped reason=not_s3")
        return 0
    try:
        existing = client.get_bucket_lifecycle_configuration(Bucket=bucket)
        rules = list(existing.get("Rules") or [])
    except Exception as exc:
        code = getattr(exc, "response", {}).get("Error", {}).get("Code", "")
        if code in {"NoSuchLifecycleConfiguration", "NoSuchBucket"}:
            rules = []
        else:
            print(f"lifecycle_status=permission_or_read_error bucket={bucket} error={exc}")
            print(
                "aws_cli=aws s3api put-bucket-lifecycle-configuration "
                f"--bucket {bucket} --lifecycle-configuration file://guest-lifecycle.json"
            )
            print("guest-lifecycle.json=" + json.dumps({"Rules": [_guest_rule()]}, sort_keys=True))
            return 2
    rules = [rule for rule in rules if str(rule.get("ID") or "") != RULE_ID]
    rules.append(_guest_rule())
    config = {"Rules": rules}
    try:
        client.put_bucket_lifecycle_configuration(
            Bucket=bucket,
            LifecycleConfiguration=config,
        )
    except Exception as exc:
        print(f"lifecycle_status=permission_or_write_error bucket={bucket} error={exc}")
        print(
            "aws_cli=aws s3api put-bucket-lifecycle-configuration "
            f"--bucket {bucket} --lifecycle-configuration file://guest-lifecycle.json"
        )
        print("guest-lifecycle.json=" + json.dumps(config, sort_keys=True))
        return 2
    print(f"lifecycle_status=applied bucket={bucket} rule_id={RULE_ID} rules={len(rules)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
