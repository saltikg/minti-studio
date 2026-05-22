from __future__ import annotations

import os
from pathlib import Path

import boto3
from dotenv import load_dotenv
from flask import Flask, jsonify


load_dotenv()

app = Flask(__name__)


def required_env(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value


@app.get("/")
def index():
    return jsonify(
        {
            "service": "minti-s3-helper",
            "status": "ready",
            "download_route": "/download",
        }
    )


@app.get("/download")
def download():
    bucket = required_env("S3_BUCKET")
    key = required_env("S3_KEY")
    local_path = Path(required_env("S3_LOCAL_PATH"))

    local_path.parent.mkdir(parents=True, exist_ok=True)

    session = boto3.session.Session(
        aws_access_key_id=required_env("AWS_ACCESS_KEY_ID"),
        aws_secret_access_key=required_env("AWS_SECRET_ACCESS_KEY"),
        region_name=required_env("AWS_DEFAULT_REGION"),
        aws_session_token=os.getenv("AWS_SESSION_TOKEN") or None,
    )

    session.client("s3").download_file(bucket, key, str(local_path))

    stat = local_path.stat()
    return jsonify(
        {
            "downloaded": True,
            "bucket": bucket,
            "key": key,
            "local_path": str(local_path),
            "size_bytes": stat.st_size,
        }
    )


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5055, debug=True)
