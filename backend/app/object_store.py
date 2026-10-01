"""
Object storage abstraction.

All original images and control-matrix-derived payloads are stored under
CONTENT-ADDRESSED keys (sha256 of the bytes) and the store refuses to
overwrite an existing object under the same key with different bytes.
Derived tiles/reports use explicit key names that embed image digest,
matrix digest, algorithm and generation, so a matrix revision produces a
fresh key namespace — nothing can be served "half old, half new".
"""

from __future__ import annotations

import hashlib
import io
import os
import threading
from typing import Protocol

from .config import get_settings


class ObjectExistsError(RuntimeError):
    """Raised when a put would overwrite an existing object with new bytes."""


def put_idempotent(store: ObjectStore, key: str, data: bytes,
                   content_type: str = "application/octet-stream") -> bool:
    """
    Put that tolerates the crash-retry pattern 'bytes written, DB commit lost'.

    Returns True if the bytes were newly written, False if identical bytes
    already existed. Differing bytes under the same key raises ObjectExistsError.
    """
    if store.exists(key):
        if sha256_hex(store.get(key)) == sha256_hex(data):
            return False
        raise ObjectExistsError(
            f"immutable object {key!r} already exists with different content"
        )
    store.put(key, data, content_type=content_type)
    return True


class ObjectStore(Protocol):
    def put(self, key: str, data: bytes, content_type: str = "application/octet-stream") -> str: ...

    def get(self, key: str) -> bytes: ...

    def exists(self, key: str) -> bool: ...

    def head_content_type(self, key: str) -> str | None: ...


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class LocalStore:
    def __init__(self, root: str) -> None:
        self.root = os.path.abspath(root)
        os.makedirs(self.root, exist_ok=True)
        self._lock = threading.Lock()

    def _path(self, key: str) -> str:
        # keys are constructed internally; normalize to prevent escape
        safe = os.path.normpath(os.path.join(self.root, key))
        if not safe.startswith(self.root + os.sep) and safe != self.root:
            raise ValueError("unsafe object key")
        return safe

    def exists(self, key: str) -> bool:
        return os.path.exists(self._path(key))

    def put(self, key: str, data: bytes, content_type: str = "application/octet-stream") -> str:
        path = self._path(key)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        digest = hashlib.sha256(data).hexdigest()
        with self._lock:
            # check-and-write is atomic across threads/processes using the
            # same local store (TOCTOU-safe within this node)
            if os.path.exists(path):
                with open(path, "rb") as fh:
                    existing_digest = hashlib.sha256(fh.read()).hexdigest()
                if existing_digest != digest:
                    raise ObjectExistsError(
                        f"immutable object {key!r} already exists with different content"
                    )
                return key  # idempotent same-content write
            tmp = f"{path}.tmp.{os.getpid()}.{threading.get_ident()}"
            with open(tmp, "wb") as fh:
                fh.write(data)
            os.replace(tmp, path)
        return key

    def get(self, key: str) -> bytes:
        with open(self._path(key), "rb") as fh:
            return fh.read()

    def head_content_type(self, key: str) -> str | None:
        return None if not self.exists(key) else "application/octet-stream"


class MinioStore:
    def __init__(self) -> None:
        from minio import Minio  # imported lazily so tests run without the server

        s = get_settings()
        self.client = Minio(
            s.minio_endpoint,
            access_key=s.minio_access_key,
            secret_key=s.minio_secret_key,
            secure=s.minio_secure,
        )
        self.bucket = s.minio_bucket
        if not self.client.bucket_exists(self.bucket):
            self.client.make_bucket(self.bucket)

    def exists(self, key: str) -> bool:
        from minio.error import S3Error

        try:
            self.client.stat_object(self.bucket, key)
            return True
        except S3Error as exc:
            if exc.code in {"NoSuchKey", "NoSuchObject"}:
                return False
            raise

    def put(self, key: str, data: bytes, content_type: str = "application/octet-stream") -> str:

        # Guard: immutability check before any write.
        if self.exists(key):
            existing = self.client.get_object(self.bucket, key).read()
            if sha256_hex(existing) != sha256_hex(data):
                raise ObjectExistsError(
                    f"immutable object {key!r} already exists with different content"
                )
            return key
        self.client.put_object(
            self.bucket, key, io.BytesIO(data), len(data), content_type=content_type
        )
        return key

    def get(self, key: str) -> bytes:
        return self.client.get_object(self.bucket, key).read()

    def head_content_type(self, key: str) -> str | None:
        from minio.error import S3Error

        try:
            return self.client.stat_object(self.bucket, key).content_type
        except S3Error as exc:
            if exc.code in {"NoSuchKey", "NoSuchObject"}:
                return None
            raise


_store: ObjectStore | None = None


def get_store() -> ObjectStore:
    global _store
    if _store is None:
        s = get_settings()
        if s.object_store == "minio":
            _store = MinioStore()
        else:
            _store = LocalStore(s.local_store_dir)
    return _store


# ---------------------------------------------------------------------------
# Key layout. Every derived key embeds the pinned digests and generation,
# which is exactly what makes a cache entry safe: matrix change => new digest
# => new key namespace; browser caches and tile stores miss in lockstep.
# ---------------------------------------------------------------------------


def raw_image_key(digest: str) -> str:
    return f"raw/images/{digest}.npy"


def matrix_object_key(digest: str) -> str:
    return f"raw/matrices/{digest}.npz"


def raw_pyramid_key(digest: str) -> str:
    return f"raw/pyramids/{digest}.npz"


def tile_cache_key(
    image_digest: str,
    matrix_digest: str,
    algorithm: str,
    generation_id: int,
    level: int,
    tx: int,
    ty: int,
    view: str,
) -> str:
    """
    Cache key for one tile view. view identifies the layer, e.g. 'raw2',
    'components1' or 'residual0'. The namespace includes image digest, matrix
    digest, algorithm and generation, so a matrix revision invalidates browser
    cache, tile store and reports atomically.
    """
    return (
        f"tiles/img-{image_digest[:16]}/mat-{matrix_digest[:16]}/{algorithm}"
        f"/gen{generation_id:06d}/L{level}/{view}/{tx:06d}_{ty:06d}.png"
    )


def ground_truth_key(image_digest: str) -> str:
    return f"raw/groundtruth/{image_digest}.npz"


def report_key(image_digest: str, matrix_digest: str, algorithm: str, report_digest: str) -> str:
    return (
        f"reports/img-{image_digest[:16]}/mat-{matrix_digest[:16]}"
        f"/{algorithm}/{report_digest}.json"
    )
