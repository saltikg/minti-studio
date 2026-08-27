import mimetypes
import os
import shutil
import tempfile
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from pathlib import Path
from typing import Callable
from typing import Iterable, List, Optional
from urllib.parse import quote

from app.video_shorts.config import (
    AWS_ACCESS_KEY_ID,
    AWS_REGION,
    AWS_SECRET_ACCESS_KEY,
    CLOUDFRONT_DOMAIN,
    CLOUDFRONT_DISTRIBUTION_ID,
    CLOUDFRONT_KEY_PAIR_ID,
    CLOUDFRONT_PRIVATE_KEY_PATH,
    MEDIA_BACKEND,
    S3_BUCKET_NAME,
    STATIC_USER_AUDIO_DIR,
    STATIC_USER_IMAGES_DIR,
    STATIC_USER_PODCASTS_DIR,
)

try:
    import boto3
    from botocore.exceptions import BotoCoreError, ClientError
    from botocore.signers import CloudFrontSigner
except ImportError:  # pragma: no cover
    boto3 = None
    BotoCoreError = ClientError = Exception
    CloudFrontSigner = None

try:
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import padding
except ImportError:  # pragma: no cover
    hashes = serialization = padding = None


_LOCAL_STATIC_ROOT = STATIC_USER_IMAGES_DIR.parent.resolve()
_MIGRATED_PREFIXES = ("user_images/", "user_audio/", "user_podcasts/")
_STORAGE_REFERENCE_PREFIX = "s3://"


def _aws_session(region_name: Optional[str] = None):
    if boto3 is None:
        raise RuntimeError("boto3 is required for AWS-backed media storage")
    return boto3.session.Session(
        aws_access_key_id=AWS_ACCESS_KEY_ID or os.getenv("AWS_ACCESS_KEY_ID"),
        aws_secret_access_key=AWS_SECRET_ACCESS_KEY or os.getenv("AWS_SECRET_ACCESS_KEY"),
        region_name=region_name or None,
    )


def _require_cloudfront_config() -> tuple[str, str, Path]:
    domain = str(CLOUDFRONT_DOMAIN or "").strip()
    key_pair_id = str(CLOUDFRONT_KEY_PAIR_ID or "").strip()
    private_key_path_raw = str(CLOUDFRONT_PRIVATE_KEY_PATH or "").strip()
    missing = []
    if not domain:
        missing.append("CLOUDFRONT_DOMAIN")
    if not key_pair_id:
        missing.append("CLOUDFRONT_KEY_PAIR_ID")
    if not private_key_path_raw:
        missing.append("CLOUDFRONT_PRIVATE_KEY_PATH")
    if missing:
        raise RuntimeError(
            "CloudFront viewer URL signing is enabled for MEDIA_BACKEND=s3 but required env vars are missing: "
            + ", ".join(missing)
        )
    private_key_path = Path(private_key_path_raw)
    if not private_key_path.exists() or not private_key_path.is_file():
        raise RuntimeError(
            f"CloudFront private key file does not exist or is not a file: {private_key_path}"
        )
    return domain, key_pair_id, private_key_path


@lru_cache(maxsize=1)
def _load_cloudfront_signer() -> tuple[str, Callable[..., str]]:
    if CloudFrontSigner is None or serialization is None or hashes is None or padding is None:
        raise RuntimeError("CloudFront signed URLs require botocore CloudFrontSigner and cryptography.")
    domain, key_pair_id, private_key_path = _require_cloudfront_config()
    private_key = serialization.load_pem_private_key(
        private_key_path.read_bytes(),
        password=None,
    )

    def rsa_signer(message: bytes) -> bytes:
        return private_key.sign(message, padding.PKCS1v15(), hashes.SHA1())

    return domain, CloudFrontSigner(key_pair_id, rsa_signer)


class ManagedTempPath(os.PathLike[str]):
    def __init__(self, path: Path):
        self.path = Path(path)

    def __fspath__(self) -> str:
        return str(self.path)

    def __str__(self) -> str:
        return str(self.path)

    def __repr__(self) -> str:
        return f"ManagedTempPath({self.path!r})"

    def __enter__(self) -> Path:
        return self.path

    def __exit__(self, exc_type, exc, tb) -> None:
        self.cleanup()

    def __getattr__(self, name: str):
        return getattr(self.path, name)

    def cleanup(self) -> None:
        try:
            self.path.unlink(missing_ok=True)
        except Exception:
            pass


@dataclass
class StorageEntry:
    key: str
    backend: str
    exists: bool
    local_path: Optional[Path] = None
    public_url: Optional[str] = None
    size_bytes: Optional[int] = None
    modified_at: Optional[datetime] = None


class Storage:
    backend_name = "base"

    def put_file(self, local_path: Path | str, key: str) -> None:
        raise NotImplementedError

    def put_bytes(self, data: bytes, key: str, content_type: Optional[str] = None) -> None:
        raise NotImplementedError

    def delete(self, key: str) -> None:
        raise NotImplementedError

    def exists(self, key: str) -> bool:
        raise NotImplementedError

    def public_url(self, key: str) -> str:
        raise NotImplementedError

    def download_to_temp(self, key: str) -> Path:
        raise NotImplementedError

    def list_prefix(self, prefix: str) -> List[StorageEntry]:
        raise NotImplementedError

    def read_bytes(self, key: str) -> bytes:
        raise NotImplementedError

    def resolve_local_or_s3(
        self,
        key: str,
        *,
        fallback_local_paths: Optional[Iterable[Path | str]] = None,
    ) -> StorageEntry:
        local_storage = get_media_storage("local")
        for raw_path in fallback_local_paths or ():
            if not raw_path:
                continue
            path = Path(raw_path).resolve()
            if path.exists() and path.is_file():
                return StorageEntry(
                    key=key,
                    backend="local",
                    exists=True,
                    local_path=path,
                    public_url=local_storage.public_url(key),
                    size_bytes=path.stat().st_size,
                    modified_at=datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc),
                )
        if self.backend_name != "local":
            try:
                remote_exists = self.exists(key)
            except Exception:
                remote_exists = False
            if remote_exists:
                return StorageEntry(
                    key=key,
                    backend=self.backend_name,
                    exists=True,
                    public_url=self.public_url(key),
                )
        return StorageEntry(key=key, backend=self.backend_name, exists=False)


class LocalStorage(Storage):
    backend_name = "local"

    def __init__(self, root_dir: Path):
        self.root_dir = root_dir.resolve()

    def _key_path(self, key: str) -> Path:
        safe_key = str(key or "").lstrip("/")
        path = (self.root_dir / safe_key).resolve()
        path.relative_to(self.root_dir)
        return path

    def put_file(self, local_path: Path | str, key: str) -> None:
        src = Path(local_path).resolve()
        dest = self._key_path(key)
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src, dest)

    def put_bytes(self, data: bytes, key: str, content_type: Optional[str] = None) -> None:
        dest = self._key_path(key)
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(data)

    def delete(self, key: str) -> None:
        path = self._key_path(key)
        if path.exists() and path.is_file():
            path.unlink()

    def exists(self, key: str) -> bool:
        path = self._key_path(key)
        return path.exists() and path.is_file()

    def public_url(self, key: str) -> str:
        return f"/video_shorts/static/{quote(str(key).lstrip('/'), safe='/')}"

    def download_to_temp(self, key: str) -> Path:
        return self._key_path(key)

    def list_prefix(self, prefix: str) -> List[StorageEntry]:
        base = self._key_path(prefix)
        if not base.exists() or not base.is_dir():
            return []
        rows: List[StorageEntry] = []
        for candidate in sorted(base.iterdir(), key=lambda p: p.stat().st_mtime, reverse=True):
            if not candidate.is_file():
                continue
            rel_key = candidate.relative_to(self.root_dir).as_posix()
            stat = candidate.stat()
            rows.append(
                StorageEntry(
                    key=rel_key,
                    backend="local",
                    exists=True,
                    local_path=candidate,
                    public_url=self.public_url(rel_key),
                    size_bytes=stat.st_size,
                    modified_at=datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc),
                )
            )
        return rows

    def read_bytes(self, key: str) -> bytes:
        return self._key_path(key).read_bytes()


class S3Storage(Storage):
    backend_name = "s3"

    def __init__(self, bucket_name: str, region_name: str):
        if boto3 is None:
            raise RuntimeError("boto3 is required for MEDIA_BACKEND=s3")
        if not bucket_name:
            raise RuntimeError("S3_BUCKET_NAME must be set for MEDIA_BACKEND=s3")
        session = _aws_session(region_name)
        self.bucket_name = bucket_name
        self.region_name = region_name or "us-east-1"
        self.client = session.client("s3", region_name=self.region_name)

    def put_file(self, local_path: Path | str, key: str) -> None:
        extra_args = {}
        guessed_type, _ = mimetypes.guess_type(str(local_path))
        if guessed_type:
            extra_args["ContentType"] = guessed_type
        kwargs = {}
        if extra_args:
            kwargs["ExtraArgs"] = extra_args
        self.client.upload_file(str(local_path), self.bucket_name, key, **kwargs)

    def put_bytes(self, data: bytes, key: str, content_type: Optional[str] = None) -> None:
        kwargs = {"Bucket": self.bucket_name, "Key": key, "Body": data}
        if content_type:
            kwargs["ContentType"] = content_type
        self.client.put_object(**kwargs)

    def delete(self, key: str) -> None:
        self.client.delete_object(Bucket=self.bucket_name, Key=key)

    def exists(self, key: str) -> bool:
        try:
            self.client.head_object(Bucket=self.bucket_name, Key=key)
            return True
        except ClientError as exc:
            code = str(exc.response.get("Error", {}).get("Code") or "")
            if code in {"403", "404", "AccessDenied", "NoSuchKey", "NotFound"}:
                return False
            raise

    def public_url(self, key: str) -> str:
        domain, signer = _load_cloudfront_signer()
        expires_at = datetime.now(timezone.utc) + timedelta(seconds=3600)
        url = f"https://{domain}/{quote(str(key).lstrip('/'), safe='/')}"
        return signer.generate_presigned_url(
            url,
            date_less_than=expires_at,
        )

    def download_to_temp(self, key: str) -> Path:
        suffix = Path(key).suffix
        temp_dir = Path(__file__).resolve().parents[1] / "tmp"
        temp_dir.mkdir(parents=True, exist_ok=True)
        handle = tempfile.NamedTemporaryFile(delete=False, suffix=suffix, dir=str(temp_dir))
        handle.close()
        temp_path = Path(handle.name)
        self.client.download_file(self.bucket_name, key, str(temp_path))
        return ManagedTempPath(temp_path)

    def list_prefix(self, prefix: str) -> List[StorageEntry]:
        paginator = self.client.get_paginator("list_objects_v2")
        rows: List[StorageEntry] = []
        try:
            for page in paginator.paginate(Bucket=self.bucket_name, Prefix=prefix):
                for item in page.get("Contents", []):
                    key = str(item.get("Key") or "")
                    if not key or key.endswith("/"):
                        continue
                    last_modified = item.get("LastModified")
                    if isinstance(last_modified, datetime) and last_modified.tzinfo is None:
                        last_modified = last_modified.replace(tzinfo=timezone.utc)
                    rows.append(
                        StorageEntry(
                            key=key,
                            backend="s3",
                            exists=True,
                            public_url=self.public_url(key),
                            size_bytes=int(item.get("Size") or 0),
                            modified_at=last_modified,
                        )
                    )
        except ClientError as exc:
            code = str(exc.response.get("Error", {}).get("Code") or "")
            if code in {"403", "AccessDenied"}:
                return []
            raise
        rows.sort(key=lambda entry: entry.modified_at or datetime.fromtimestamp(0, tz=timezone.utc), reverse=True)
        return rows

    def read_bytes(self, key: str) -> bytes:
        response = self.client.get_object(Bucket=self.bucket_name, Key=key)
        return response["Body"].read()


_STORAGE_INSTANCES: dict[str, Storage] = {}


def get_media_storage(backend: Optional[str] = None) -> Storage:
    target = (backend or MEDIA_BACKEND or "local").strip().lower() or "local"
    if target not in {"local", "s3"}:
        target = "local"
    if target not in _STORAGE_INSTANCES:
        if target == "s3":
            _STORAGE_INSTANCES[target] = S3Storage(S3_BUCKET_NAME, AWS_REGION)
        else:
            _STORAGE_INSTANCES[target] = LocalStorage(_LOCAL_STATIC_ROOT)
    return _STORAGE_INSTANCES[target]


def is_media_storage_key(key: str) -> bool:
    clean_key = str(key or "").lstrip("/")
    return clean_key.startswith(_MIGRATED_PREFIXES)


def build_storage_reference(key: str) -> str:
    clean_key = str(key or "").lstrip("/")
    return f"{_STORAGE_REFERENCE_PREFIX}{clean_key}" if clean_key else ""


def is_storage_reference(value: str) -> bool:
    return str(value or "").strip().startswith(_STORAGE_REFERENCE_PREFIX)


def storage_reference_key(value: str) -> str:
    raw_value = str(value or "").strip()
    if not raw_value.startswith(_STORAGE_REFERENCE_PREFIX):
        return ""
    return raw_value[len(_STORAGE_REFERENCE_PREFIX) :].lstrip("/")


def public_url_for_stored_media(value: str, fallback_local_url: Optional[str] = None) -> str:
    raw_value = str(value or "").strip()
    if not raw_value:
        return fallback_local_url or ""
    if not is_storage_reference(raw_value):
        return raw_value
    key = storage_reference_key(raw_value)
    if not key:
        return fallback_local_url or ""
    return get_media_storage().public_url(key)


def resolve_stored_media(
    value: str,
    *,
    fallback_local_paths: Optional[Iterable[Path | str]] = None,
) -> StorageEntry:
    raw_value = str(value or "").strip()
    if not is_storage_reference(raw_value):
        return StorageEntry(key=raw_value, backend="unknown", exists=False)
    key = storage_reference_key(raw_value)
    if not key:
        return StorageEntry(key="", backend="unknown", exists=False)
    return get_media_storage().resolve_local_or_s3(
        key,
        fallback_local_paths=fallback_local_paths,
    )


def invalidate_cloudfront_paths(paths: Iterable[str]) -> Optional[str]:
    distribution_id = str(CLOUDFRONT_DISTRIBUTION_ID or "").strip()
    if not distribution_id:
        return None

    clean_paths = []
    for raw_path in paths or ():
        path = "/" + str(raw_path or "").lstrip("/")
        if path == "/":
            continue
        clean_paths.append(path)
    if not clean_paths:
        return None

    session = _aws_session(AWS_REGION)
    client = session.client("cloudfront")
    caller_reference = f"{clean_paths[0]}:{datetime.now(timezone.utc).isoformat()}"
    response = client.create_invalidation(
        DistributionId=distribution_id,
        InvalidationBatch={
            "Paths": {"Quantity": len(clean_paths), "Items": clean_paths},
            "CallerReference": caller_reference,
        },
    )
    invalidation = response.get("Invalidation", {})
    return str(invalidation.get("Id") or "").strip() or None
