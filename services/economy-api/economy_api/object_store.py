from __future__ import annotations

import hashlib
import io
import json
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Protocol

from .crawler_status import decode_crawler_status
from .snapshot_ingest import POINTER_SCHEMA, REVISION_RE, SHA256_RE, VIEW_NAME_RE

_PART_RE = re.compile(r"^[a-z0-9_-]{1,48}$")
_PUBLICATION_ID_RE = re.compile(r"^[0-9a-f]{32}$")
_MAX_DOCUMENT_BYTES = 8 * 1024 * 1024
_MAX_POINTER_BYTES = 128 * 1024


class ObjectClient(Protocol):
    def get_object(self, *, Bucket: str, Key: str) -> dict[str, Any]: ...

    def put_object(
        self,
        *,
        Bucket: str,
        Key: str,
        Body: bytes,
        Metadata: dict[str, str],
        IfMatch: str | None = None,
        IfNoneMatch: str | None = None,
    ) -> dict[str, Any]: ...


@dataclass(frozen=True, slots=True)
class _Cached:
    expires_at: float
    value: dict[str, Any]


class _ObjectNotFound(Exception):
    pass


class _ObjectConflict(Exception):
    pass


class MetadataIamTokenProvider:
    """Short-lived function service-account token, never persisted or logged."""

    _URL = (
        "http://169.254.169.254/computeMetadata/v1/"
        "instance/service-accounts/default/token"
    )

    def __init__(self, *, clock: Any = time.monotonic) -> None:
        self.clock = clock
        self._lock = threading.Lock()
        self._token = ""
        self._expires_at = 0.0

    def __call__(self) -> str:
        now = float(self.clock())
        with self._lock:
            if self._token and self._expires_at > now + 30:
                return self._token
            request = urllib.request.Request(
                self._URL, headers={"Metadata-Flavor": "Google"}
            )
            with urllib.request.urlopen(request, timeout=2) as response:
                payload = json.loads(response.read(16_384))
            token = payload.get("access_token")
            expires_in = payload.get("expires_in", 300)
            if not isinstance(token, str) or not token:
                raise RuntimeError("metadata_iam_token_missing")
            self._token = token
            self._expires_at = now + max(60, min(int(expires_in), 3600))
            return token


class IamObjectClient:
    """Minimal Object Storage client using the function service-account IAM path."""

    def __init__(self, token_provider: Any | None = None) -> None:
        self.token_provider = token_provider or MetadataIamTokenProvider()

    def get_object(self, *, Bucket: str, Key: str) -> dict[str, Any]:
        bucket = urllib.parse.quote(Bucket, safe="")
        key = urllib.parse.quote(Key, safe="/")
        request = urllib.request.Request(
            f"https://storage.yandexcloud.net/{bucket}/{key}",
            headers={"Authorization": f"Bearer {self.token_provider()}"},
        )
        try:
            response = urllib.request.urlopen(request, timeout=8)
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                raise _ObjectNotFound from exc
            raise
        body = response.read(_MAX_DOCUMENT_BYTES + 1)
        metadata = {}
        digest = response.headers.get("x-amz-meta-sha256")
        if digest:
            metadata["sha256"] = digest
        return {
            "Body": io.BytesIO(body),
            "Metadata": metadata,
            "ETag": response.headers.get("ETag"),
        }

    def put_object(
        self,
        *,
        Bucket: str,
        Key: str,
        Body: bytes,
        Metadata: dict[str, str],
        IfMatch: str | None = None,
        IfNoneMatch: str | None = None,
    ) -> dict[str, Any]:
        if (IfMatch is None) == (IfNoneMatch is None):
            raise ValueError("exactly_one_write_condition_required")
        bucket = urllib.parse.quote(Bucket, safe="")
        key = urllib.parse.quote(Key, safe="/")
        headers = {
            "Authorization": f"Bearer {self.token_provider()}",
            "Content-Type": "application/json; charset=utf-8",
            "Content-Length": str(len(Body)),
        }
        for name, value in Metadata.items():
            if not re.fullmatch(r"[a-z0-9-]{1,64}", name) or len(value) > 256:
                raise ValueError("object_metadata_invalid")
            headers[f"X-Amz-Meta-{name}"] = value
        if IfMatch is not None:
            headers["If-Match"] = IfMatch
        if IfNoneMatch is not None:
            headers["If-None-Match"] = IfNoneMatch
        request = urllib.request.Request(
            f"https://storage.yandexcloud.net/{bucket}/{key}",
            data=Body,
            method="PUT",
            headers=headers,
        )
        try:
            response = urllib.request.urlopen(request, timeout=12)
        except urllib.error.HTTPError as exc:
            if exc.code in {409, 412}:
                raise _ObjectConflict from exc
            raise
        return {"ETag": response.headers.get("ETag")}


class ObjectStorageEconomyStore:
    """Reads sanitized current snapshots from a private Object Storage bucket.

    The publisher writes immutable ``versions/<revision>`` and atomically
    replaces one ``current.json`` manifest. The API resolves a view only through
    that manifest, so concurrent publication cannot expose mixed revisions.
    """

    def __init__(
        self,
        bucket: str,
        *,
        client: ObjectClient | None = None,
        cache_seconds: int = 30,
        clock: Any = time.monotonic,
    ) -> None:
        if not bucket:
            raise ValueError("snapshot_bucket_required")
        if client is None:
            client = IamObjectClient()
        self.bucket = bucket
        self.client = client
        self.cache_seconds = max(0, min(cache_seconds, 60))
        self.clock = clock
        self._cache: dict[tuple[str, str], _Cached] = {}
        self._lock = threading.Lock()

    def _get(self, key: str, *, expected_sha256: str | None = None) -> bytes:
        body, _etag = self._get_record(key, expected_sha256=expected_sha256)
        return body

    def _get_record(
        self, key: str, *, expected_sha256: str | None = None
    ) -> tuple[bytes, str | None]:
        try:
            response = self.client.get_object(Bucket=self.bucket, Key=key)
        except _ObjectNotFound:
            raise
        except Exception as exc:
            error = getattr(exc, "response", {}).get("Error", {}).get("Code")
            if str(error) in {"NoSuchKey", "404", "NotFound"}:
                raise _ObjectNotFound from exc
            raise
        body = response["Body"].read(_MAX_DOCUMENT_BYTES + 1)
        if len(body) > _MAX_DOCUMENT_BYTES:
            raise RuntimeError("economy_view_exceeds_8mb_limit")
        metadata_digest = response.get("Metadata", {}).get("sha256")
        actual = hashlib.sha256(body).hexdigest()
        if metadata_digest and metadata_digest != actual:
            raise RuntimeError("economy_view_hash_mismatch")
        if expected_sha256 is not None and expected_sha256 != actual:
            raise RuntimeError("economy_pointer_hash_mismatch")
        etag = response.get("ETag")
        if etag is not None and (not isinstance(etag, str) or len(etag) > 256):
            raise RuntimeError("economy_object_etag_invalid")
        return body, etag

    def _put(
        self,
        key: str,
        body: bytes,
        digest: str,
        *,
        expected_etag: str | None,
    ) -> None:
        try:
            self.client.put_object(
                Bucket=self.bucket,
                Key=key,
                Body=body,
                Metadata={"sha256": digest},
                IfMatch=expected_etag,
                IfNoneMatch="*" if expected_etag is None else None,
            )
        except _ObjectConflict:
            raise
        except Exception as exc:
            error = getattr(exc, "response", {}).get("Error", {}).get("Code")
            if str(error) in {"PreconditionFailed", "ConditionalRequestConflict", "409", "412"}:
                raise _ObjectConflict from exc
            raise

    @staticmethod
    def _pointer(body: bytes) -> dict[str, Any]:
        if not body or len(body) > _MAX_POINTER_BYTES:
            raise RuntimeError("invalid_economy_pointer")
        try:
            pointer = json.loads(body)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise RuntimeError("invalid_economy_pointer") from exc
        if (
            not isinstance(pointer, dict)
            or pointer.get("schemaVersion") != POINTER_SCHEMA
            or not isinstance(pointer.get("revision"), str)
            or not REVISION_RE.fullmatch(pointer["revision"])
            or not isinstance(pointer.get("views"), dict)
        ):
            raise RuntimeError("invalid_economy_pointer")
        publication_id = pointer.get("publicationId")
        if publication_id is not None and (
            not isinstance(publication_id, str)
            or _PUBLICATION_ID_RE.fullmatch(publication_id) is None
        ):
            raise RuntimeError("invalid_economy_pointer")
        return pointer

    def _current_record(self) -> tuple[bytes, str | None, dict[str, Any]] | None:
        try:
            body, etag = self._get_record("snapshots/current.json")
        except _ObjectNotFound:
            return None
        return body, etag, self._pointer(body)

    def _resolve(self, view: str, segment: str) -> tuple[str, str, str]:
        body = self._get("snapshots/current.json")
        pointer = self._pointer(body)
        entry = pointer["views"].get(f"{view}.{segment}")
        if not isinstance(entry, dict) or set(entry) != {"key", "sha256"}:
            raise _ObjectNotFound
        revision = pointer["revision"]
        expected_key = f"snapshots/versions/{revision}/{view}/{segment}.json"
        if entry.get("key") != expected_key or not SHA256_RE.fullmatch(str(entry.get("sha256", ""))):
            raise RuntimeError("invalid_economy_pointer_entry")
        return revision, expected_key, entry["sha256"]

    def read(self, view: str, segment: str) -> dict[str, Any] | None:
        if not _PART_RE.fullmatch(view) or not _PART_RE.fullmatch(segment):
            return None
        cache_key = (view, segment)
        now = float(self.clock())
        with self._lock:
            cached = self._cache.get(cache_key)
            if cached and cached.expires_at > now:
                return dict(cached.value)
        try:
            revision, key, expected = self._resolve(view, segment)
            body = self._get(key, expected_sha256=expected)
        except _ObjectNotFound:
            return None
        value = json.loads(body)
        if not isinstance(value, dict) or value.get("revision") != revision:
            raise RuntimeError("invalid_economy_view")
        with self._lock:
            self._cache[cache_key] = _Cached(now + self.cache_seconds, value)
        return dict(value)

    def read_crawler_status(self, now: datetime) -> dict[str, Any] | None:
        try:
            revision, _key, _digest = self._resolve("market", "default")
        except _ObjectNotFound:
            return None
        try:
            document = self._get(
                f"snapshots/crawler-status/{revision}/current.json"
            )
        except _ObjectNotFound:
            observed = now.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
            return {
                "configured": True,
                "source": "standalone_object",
                "controlAvailable": False,
                "economyRevision": revision,
                "nextRunAt": None,
                "minimumNextAttemptAt": None,
                "lastSuccess": None,
                "workUnit": "server_scopes",
                "runId": None,
                "status": "failed",
                "phase": "publish",
                "revision": 0,
                "updatedAt": observed,
                "startedAt": None,
                "elapsedSeconds": 0,
                "totalItems": 100,
                "processedItems": 0,
                "successfulRequests": 0,
                "failedRequests": 1,
                "retriedRequests": 0,
                "queueDepth": 100,
                "checkpoint": None,
                "errors": [
                    {
                        "errorId": "standalone-crawler_status_missing_current_revision",
                        "at": observed,
                        "code": "crawler_status_missing_current_revision",
                        "errorClass": "storage",
                        "message": "crawler_status_missing_current_revision",
                        "retryable": True,
                    }
                ],
            }
        try:
            confirmed_revision, _confirmed_key, _confirmed_digest = self._resolve(
                "market", "default"
            )
        except _ObjectNotFound:
            return None
        if confirmed_revision != revision:
            raise RuntimeError("crawler_status_revision_race")
        return decode_crawler_status(document, revision, now=now)

    def current_revision(self) -> str | None:
        record = self._current_record()
        return None if record is None else str(record[2]["revision"])

    def stage_view(
        self, revision: str, name: str, encoded: bytes, digest: str
    ) -> None:
        matched = VIEW_NAME_RE.fullmatch(name)
        if matched is None or hashlib.sha256(encoded).hexdigest() != digest:
            raise ValueError("snapshot_view_invalid")
        view, segment = matched.groups()
        key = f"snapshots/versions/{revision}/{view}/{segment}.json"
        try:
            existing = self._get(key, expected_sha256=digest)
        except _ObjectNotFound:
            existing = None
        if existing is not None:
            if existing != encoded:
                raise ValueError("snapshot_immutable_conflict")
            return
        try:
            self._put(key, encoded, digest, expected_etag=None)
        except _ObjectConflict:
            try:
                concurrent = self._get(key, expected_sha256=digest)
            except _ObjectNotFound as exc:
                raise ValueError("snapshot_immutable_conflict") from exc
            if concurrent != encoded:
                raise ValueError("snapshot_immutable_conflict")
        readback = self._get(key, expected_sha256=digest)
        if readback != encoded:
            raise RuntimeError("snapshot_view_readback_mismatch")

    def commit_views(
        self,
        revision: str,
        expected_current_revision: str | None,
        views: dict[str, str],
        publication_id: str,
    ) -> dict[str, Any]:
        pointer_views: dict[str, dict[str, str]] = {}
        for name, digest in sorted(views.items()):
            matched = VIEW_NAME_RE.fullmatch(name)
            if matched is None:
                raise ValueError("snapshot_view_name_invalid")
            view, segment = matched.groups()
            key = f"snapshots/versions/{revision}/{view}/{segment}.json"
            self._get(key, expected_sha256=digest)
            pointer_views[f"{view}.{segment}"] = {"key": key, "sha256": digest}
        pointer_value = {
            "schemaVersion": POINTER_SCHEMA,
            "revision": revision,
            "publicationId": publication_id,
            "views": pointer_views,
        }
        pointer = json.dumps(
            pointer_value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        record = self._current_record()
        current_revision = None if record is None else str(record[2]["revision"])
        if current_revision == revision:
            current_views = record[2].get("views") if record is not None else None
            if current_views != pointer_views:
                raise ValueError("snapshot_revision_content_conflict")
            return {
                "revision": revision,
                "views": len(views),
                "hashes": dict(sorted(views.items())),
                "idempotent": True,
            }
        if current_revision != expected_current_revision:
            raise ValueError("snapshot_revision_conflict")
        expected_etag = None if record is None else record[1]
        if record is not None and not expected_etag:
            raise RuntimeError("snapshot_current_etag_missing")
        try:
            self._put(
                "snapshots/current.json",
                pointer,
                hashlib.sha256(pointer).hexdigest(),
                expected_etag=expected_etag,
            )
        except _ObjectConflict as exc:
            raise ValueError("snapshot_revision_conflict") from exc
        readback = self._current_record()
        if readback is None or readback[0] != pointer:
            raise RuntimeError("snapshot_pointer_readback_mismatch")
        for name, digest in sorted(views.items()):
            matched = VIEW_NAME_RE.fullmatch(name)
            assert matched is not None
            view, segment = matched.groups()
            self._get(
                f"snapshots/versions/{revision}/{view}/{segment}.json",
                expected_sha256=digest,
            )
        with self._lock:
            self._cache.clear()
        return {
            "revision": revision,
            "views": len(views),
            "hashes": dict(sorted(views.items())),
            "idempotent": False,
        }
