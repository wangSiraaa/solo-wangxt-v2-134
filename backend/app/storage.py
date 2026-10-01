from __future__ import annotations

import hashlib
import io
import json
import os
from pathlib import Path
from typing import Any


class ImmutableObjectError(RuntimeError):
    pass


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def canonical_json(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


class ObjectStore:
    """Small immutable object storage facade used by API and workers.

    Local filesystem mode makes tests and offline development possible. MinIO
    mode never replaces an object: callers receive a fresh content-addressed
    key, and an unexpected existing key is treated as an integrity error.
    """

    def __init__(
        self,
        local_dir: str | None = None,
        endpoint: str | None = None,
        access_key: str | None = None,
        secret_key: str | None = None,
        bucket: str = "unmix",
        secure: bool = False,
    ):
        self.bucket = bucket
        self.local_dir = Path(local_dir or "/tmp/unmix-objects")
        self.local_dir.mkdir(parents=True, exist_ok=True)
        self._client = None
        if endpoint:
            # Imported lazily so unit tests do not need the MinIO SDK.
            from minio import Minio

            self._client = Minio(
                endpoint,
                access_key=access_key,
                secret_key=secret_key,
                secure=secure,
            )
            if not self._client.bucket_exists(bucket):
                self._client.make_bucket(bucket)

    def _local_path(self, key: str) -> Path:
        root = self.local_dir.resolve()
        path = (self.local_dir / key).resolve()
        if root not in path.parents and path != root:
            raise ValueError("object key escapes storage root")
        path.parent.mkdir(parents=True, exist_ok=True)
        return path

    def exists(self, key: str) -> bool:
        if self._client:
            from minio.error import S3Error

            try:
                self._client.stat_object(self.bucket, key)
                return True
            except S3Error as exc:
                if exc.code in {"NoSuchKey", "NoSuchObject"}:
                    return False
                raise
        return self._local_path(key).exists()

    def put_if_absent(self, key: str, data: bytes, content_type: str = "application/octet-stream") -> None:
        if self._client:
            from minio.error import S3Error

            try:
                self._client.stat_object(self.bucket, key)
                raise ImmutableObjectError(f"refusing to overwrite {key}")
            except S3Error as exc:
                if exc.code not in {"NoSuchKey", "NoSuchObject"}:
                    raise
            self._client.put_object(
                self.bucket, key, io.BytesIO(data), len(data), content_type=content_type
            )
            return

        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        try:
            fd = os.open(self._local_path(key), flags)
        except FileExistsError as exc:
            raise ImmutableObjectError(f"refusing to overwrite {key}") from exc
        with os.fdopen(fd, "wb") as f:
            f.write(data)

    def put_content_addressed(self, prefix: str, data: bytes, suffix: str = ".bin") -> tuple[str, str]:
        digest = sha256_bytes(data)
        key = f"{prefix.rstrip('/')}/{digest}{suffix}"
        if not self.exists(key):
            self.put_if_absent(key, data)
        return key, digest

    def put_json(self, prefix: str, value: Any) -> tuple[str, str]:
        data = canonical_json(value)
        return self.put_content_addressed(prefix, data, ".json")

    def get(self, key: str) -> bytes:
        if self._client:
            response = self._client.get_object(self.bucket, key)
            try:
                return response.read()
            finally:
                response.close()
                response.release_conn()
        return self._local_path(key).read_bytes()

    def get_json(self, key: str) -> Any:
        return json.loads(self.get(key))
