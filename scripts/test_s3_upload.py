#!/usr/bin/env python3
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv

try:
    import boto3
except ImportError as exc:  # pragma: no cover
    raise SystemExit(f"boto3 import failed: {exc}")


def main() -> int:
    project_root = Path(__file__).resolve().parents[1]
    load_dotenv(project_root / ".env")

    media_backend = (os.getenv("MEDIA_BACKEND", "local") or "local").strip().lower()
    region = (os.getenv("AWS_REGION", "") or "").strip()
    bucket = (os.getenv("S3_BUCKET_NAME", "") or "").strip()
    access_key = (os.getenv("AWS_ACCESS_KEY_ID", "") or "").strip()
    secret_key = (os.getenv("AWS_SECRET_ACCESS_KEY", "") or "").strip()

    if not bucket:
        print("FAIL: S3_BUCKET_NAME is missing")
        return 1
    if not region:
        print("FAIL: AWS_REGION is missing")
        return 1
    if not access_key or not secret_key:
        print("FAIL: AWS credentials are missing")
        return 1

    session = boto3.session.Session(
        aws_access_key_id=access_key,
        aws_secret_access_key=secret_key,
        region_name=region,
    )
    s3 = session.client("s3", region_name=region)

    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    object_key = f"test/codex_s3_probe_{ts}.txt"
    tmp_path = None
    try:
        with tempfile.NamedTemporaryFile("w", delete=False, suffix=".txt", encoding="utf-8") as handle:
            handle.write(f"codex s3 probe {ts}\n")
            tmp_path = Path(handle.name)

        print("S3 upload test")
        print(f"MEDIA_BACKEND={media_backend}")
        print(f"AWS_REGION={region}")
        print(f"S3_BUCKET_NAME={bucket}")
        print(f"uploading_key={object_key}")

        s3.upload_file(str(tmp_path), bucket, object_key, ExtraArgs={"ContentType": "text/plain"})
        presigned_url = s3.generate_presigned_url(
            "get_object",
            Params={"Bucket": bucket, "Key": object_key},
            ExpiresIn=3600,
        )
        print("SUCCESS: upload completed")
        print(f"presigned_url={presigned_url}")
        return 0
    except Exception as exc:
        print(f"FAIL: {exc}")
        return 1
    finally:
        if tmp_path and tmp_path.exists():
            try:
                tmp_path.unlink()
            except Exception:
                pass


if __name__ == "__main__":
    raise SystemExit(main())
