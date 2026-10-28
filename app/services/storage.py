"""Object storage adapter (LLD 11).

Production uses S3-compatible storage in a UK/EEA region with server-side encryption on every
object and a private bucket (NFR-04, NFR-08). Local and in-memory backends exist for development
and tests only; configuration refuses them in production.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Iterator
from functools import lru_cache
from pathlib import Path

from app.config import get_settings


class StorageBackend(ABC):
    @abstractmethod
    def put(self, key: str, data: bytes, content_type: str) -> None: ...

    @abstractmethod
    def open(self, key: str) -> Iterator[bytes]: ...

    @abstractmethod
    def delete(self, key: str) -> None: ...

    @abstractmethod
    def exists(self, key: str) -> bool: ...


class LocalStorage(StorageBackend):
    def __init__(self, root: str) -> None:
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, key: str) -> Path:
        p = (self.root / key).resolve()
        if self.root not in p.parents:
            raise ValueError("storage key escapes the storage root")
        return p

    def put(self, key: str, data: bytes, content_type: str) -> None:
        p = self._path(key)
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(p.suffix + ".part")
        tmp.write_bytes(data)
        tmp.replace(p)

    def open(self, key: str) -> Iterator[bytes]:
        with self._path(key).open("rb") as fh:
            while chunk := fh.read(64 * 1024):
                yield chunk

    def delete(self, key: str) -> None:
        self._path(key).unlink(missing_ok=True)

    def exists(self, key: str) -> bool:
        return self._path(key).exists()


class MemoryStorage(StorageBackend):
    def __init__(self) -> None:
        self.objects: dict[str, tuple[bytes, str]] = {}

    def put(self, key: str, data: bytes, content_type: str) -> None:
        self.objects[key] = (data, content_type)

    def open(self, key: str) -> Iterator[bytes]:
        yield self.objects[key][0]

    def delete(self, key: str) -> None:
        self.objects.pop(key, None)

    def exists(self, key: str) -> bool:
        return key in self.objects


class S3Storage(StorageBackend):
    def __init__(self, bucket: str, region: str, endpoint_url: str | None = None) -> None:
        import boto3

        self.bucket = bucket
        self.client = boto3.client("s3", region_name=region, endpoint_url=endpoint_url)

    def put(self, key: str, data: bytes, content_type: str) -> None:
        self.client.put_object(
            Bucket=self.bucket,
            Key=key,
            Body=data,
            ContentType=content_type,
            ServerSideEncryption="AES256",  # SSE on every object (LLD 11.1 step 4)
        )

    def open(self, key: str) -> Iterator[bytes]:
        obj = self.client.get_object(Bucket=self.bucket, Key=key)
        yield from obj["Body"].iter_chunks(64 * 1024)

    def delete(self, key: str) -> None:
        self.client.delete_object(Bucket=self.bucket, Key=key)

    def exists(self, key: str) -> bool:
        from botocore.exceptions import ClientError

        try:
            self.client.head_object(Bucket=self.bucket, Key=key)
            return True
        except ClientError:
            return False


@lru_cache
def get_storage() -> StorageBackend:
    s = get_settings()
    if s.STORAGE_BACKEND == "s3":
        return S3Storage(s.STORAGE_BUCKET, s.STORAGE_REGION, s.S3_ENDPOINT_URL)
    if s.STORAGE_BACKEND == "memory":
        return MemoryStorage()
    return LocalStorage(s.STORAGE_LOCAL_PATH)
