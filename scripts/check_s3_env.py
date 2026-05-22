#!/usr/bin/env python3
import os
from pathlib import Path

from dotenv import load_dotenv


def _present(name: str) -> str:
    return "yes" if (os.getenv(name) or "").strip() else "no"


def main() -> int:
    project_root = Path(__file__).resolve().parents[1]
    load_dotenv(project_root / ".env")

    media_backend = (os.getenv("MEDIA_BACKEND", "local") or "local").strip().lower()
    aws_region = (os.getenv("AWS_REGION", "") or "").strip()
    bucket_name = (os.getenv("S3_BUCKET_NAME", "") or "").strip()

    print("S3 environment check")
    print(f"MEDIA_BACKEND={media_backend or '<unset>'}")
    print(f"AWS_REGION={aws_region or '<unset>'}")
    print(f"S3_BUCKET_NAME={bucket_name or '<unset>'}")
    print(f"AWS_ACCESS_KEY_ID_present={_present('AWS_ACCESS_KEY_ID')}")
    print(f"AWS_SECRET_ACCESS_KEY_present={_present('AWS_SECRET_ACCESS_KEY')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
