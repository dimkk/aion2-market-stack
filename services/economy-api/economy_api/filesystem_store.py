from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
import threading
from datetime import datetime
from pathlib import Path
from typing import Any

from .crawler_status import decode_crawler_status
from .snapshot_ingest import POINTER_SCHEMA, REVISION_RE, SHA256_RE, VIEW_NAME_RE


_PART_RE = re.compile(r"^[a-z0-9_-]{1,48}$")
_PUBLICATION_ID_RE = re.compile(r"^[0-9a-f]{32}$")
_MAX_DOCUMENT_BYTES = 8 * 1024 * 1024
_MAX_POINTER_BYTES = 128 * 1024


class FileSystemEconomyStore:
    """Crash-safe single-instance store for the VM deployment.

    Immutable view files are fsync'ed first.  ``current.json`` is then replaced
    atomically, so readers observe either the previous complete revision or the
    new complete revision.  The self-hosted deployment intentionally runs one
    Gunicorn process with multiple threads; that makes the in-process lock the
    only publication lock required while keeping request concurrency.
    """

    def __init__(self, root: str | Path) -> None:
        candidate = Path(root)
        if candidate.is_symlink():
            raise ValueError("snapshot_root_symlink_rejected")
        self.root = candidate.resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()

    @property
    def _pointer_path(self) -> Path:
        return self.root / "current.json"

    def _view_path(self, revision: str, view: str, segment: str) -> Path:
        if REVISION_RE.fullmatch(revision) is None:
            raise ValueError("snapshot_revision_invalid")
        if _PART_RE.fullmatch(view) is None or _PART_RE.fullmatch(segment) is None:
            raise ValueError("snapshot_view_invalid")
        return self.root / "versions" / revision / view / f"{segment}.json"

    def _crawler_status_path(self, revision: str) -> Path:
        if REVISION_RE.fullmatch(revision) is None:
            raise ValueError("snapshot_revision_invalid")
        return self.root / "versions" / revision / "crawler-status.json"

    @staticmethod
    def _read_bounded(path: Path, maximum: int) -> bytes:
        if path.is_symlink():
            raise RuntimeError("snapshot_symlink_rejected")
        with path.open("rb") as stream:
            body = stream.read(maximum + 1)
        if not body or len(body) > maximum:
            raise RuntimeError("snapshot_document_size_invalid")
        return body

    @staticmethod
    def _atomic_write(path: Path, body: bytes, mode: int = 0o640) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary_name = tempfile.mkstemp(prefix=".stage-", dir=path.parent)
        temporary = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(body)
                stream.flush()
                os.fsync(stream.fileno())
            os.chmod(temporary, mode)
            os.replace(temporary, path)
            try:
                directory = os.open(path.parent, os.O_RDONLY)
            except OSError:
                directory = None
            if directory is not None:
                try:
                    try:
                        os.fsync(directory)
                    except OSError:
                        # Windows does not expose directory fsync.  Production
                        # is Linux, where this closes the rename durability gap.
                        pass
                finally:
                    os.close(directory)
        finally:
            if temporary.exists():
                temporary.unlink()

    @staticmethod
    def _decode_pointer(body: bytes) -> dict[str, Any]:
        if len(body) > _MAX_POINTER_BYTES:
            raise RuntimeError("invalid_economy_pointer")
        try:
            pointer = json.loads(body)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise RuntimeError("invalid_economy_pointer") from exc
        publication_id = pointer.get("publicationId") if isinstance(pointer, dict) else None
        if (
            not isinstance(pointer, dict)
            or pointer.get("schemaVersion") != POINTER_SCHEMA
            or not isinstance(pointer.get("revision"), str)
            or REVISION_RE.fullmatch(pointer["revision"]) is None
            or not isinstance(pointer.get("views"), dict)
            or not isinstance(publication_id, str)
            or _PUBLICATION_ID_RE.fullmatch(publication_id) is None
        ):
            raise RuntimeError("invalid_economy_pointer")
        return pointer

    def _pointer(self) -> dict[str, Any] | None:
        try:
            body = self._read_bounded(self._pointer_path, _MAX_POINTER_BYTES)
        except FileNotFoundError:
            return None
        return self._decode_pointer(body)

    def read(self, view: str, segment: str) -> dict[str, Any] | None:
        if _PART_RE.fullmatch(view) is None or _PART_RE.fullmatch(segment) is None:
            return None
        pointer = self._pointer()
        if pointer is None:
            return None
        entry = pointer["views"].get(f"{view}.{segment}")
        revision = pointer["revision"]
        expected_key = f"snapshots/versions/{revision}/{view}/{segment}.json"
        if (
            not isinstance(entry, dict)
            or set(entry) != {"key", "sha256"}
            or entry.get("key") != expected_key
            or not isinstance(entry.get("sha256"), str)
            or SHA256_RE.fullmatch(entry["sha256"]) is None
        ):
            return None
        path = self._view_path(revision, view, segment)
        try:
            body = self._read_bounded(path, _MAX_DOCUMENT_BYTES)
        except FileNotFoundError:
            return None
        if hashlib.sha256(body).hexdigest() != entry["sha256"]:
            raise RuntimeError("economy_pointer_hash_mismatch")
        value = json.loads(body)
        if not isinstance(value, dict) or value.get("revision") != revision:
            raise RuntimeError("invalid_economy_view")
        return value

    def current_revision(self) -> str | None:
        pointer = self._pointer()
        return None if pointer is None else str(pointer["revision"])

    def read_crawler_status(self, now: datetime) -> dict[str, Any] | None:
        pointer = self._pointer()
        if pointer is None:
            return None
        revision = str(pointer["revision"])
        try:
            document = self._read_bounded(
                self._crawler_status_path(revision), _MAX_POINTER_BYTES
            )
        except FileNotFoundError:
            return None
        return decode_crawler_status(document, revision, now=now)

    def write_crawler_status(
        self,
        revision: str,
        encoded: bytes,
        digest: str,
        *,
        now: datetime,
    ) -> dict[str, Any]:
        if (
            REVISION_RE.fullmatch(revision) is None
            or SHA256_RE.fullmatch(digest) is None
            or not encoded
            or len(encoded) > _MAX_POINTER_BYTES
            or hashlib.sha256(encoded).hexdigest() != digest
        ):
            raise ValueError("crawler_status_invalid")
        decoded = decode_crawler_status(encoded, revision, now=now)
        path = self._crawler_status_path(revision)
        with self._lock:
            pointer = self._pointer()
            if pointer is None or pointer.get("revision") != revision:
                raise ValueError("crawler_status_revision_conflict")
            try:
                prior_body = self._read_bounded(path, _MAX_POINTER_BYTES)
            except FileNotFoundError:
                prior_body = None
            if prior_body is not None:
                prior = json.loads(prior_body)
                candidate = json.loads(encoded)
                if str(candidate.get("observed_at_utc", "")) < str(
                    prior.get("observed_at_utc", "")
                ):
                    raise ValueError("crawler_status_superseded")
                if prior_body == encoded:
                    return {"sha256": digest, "idempotent": True, "status": decoded}
            self._atomic_write(path, encoded)
            confirmed = self._read_bounded(path, _MAX_POINTER_BYTES)
            confirmed_pointer = self._pointer()
            if (
                confirmed != encoded
                or confirmed_pointer is None
                or confirmed_pointer.get("revision") != revision
            ):
                raise RuntimeError("crawler_status_readback_mismatch")
        return {"sha256": digest, "idempotent": False, "status": decoded}

    def stage_view(self, revision: str, name: str, encoded: bytes, digest: str) -> None:
        matched = VIEW_NAME_RE.fullmatch(name)
        if (
            matched is None
            or len(encoded) > _MAX_DOCUMENT_BYTES
            or hashlib.sha256(encoded).hexdigest() != digest
        ):
            raise ValueError("snapshot_view_invalid")
        view, segment = matched.groups()
        path = self._view_path(revision, view, segment)
        with self._lock:
            try:
                current = self._read_bounded(path, _MAX_DOCUMENT_BYTES)
            except FileNotFoundError:
                current = None
            if current is not None:
                if current != encoded:
                    raise ValueError("snapshot_immutable_conflict")
                return
            self._atomic_write(path, encoded)
            if self._read_bounded(path, _MAX_DOCUMENT_BYTES) != encoded:
                raise RuntimeError("snapshot_view_readback_mismatch")

    def commit_views(
        self,
        revision: str,
        expected_current_revision: str | None,
        views: dict[str, str],
        publication_id: str,
    ) -> dict[str, Any]:
        if (
            REVISION_RE.fullmatch(revision) is None
            or _PUBLICATION_ID_RE.fullmatch(publication_id) is None
        ):
            raise ValueError("snapshot_commit_invalid")
        pointer_views: dict[str, dict[str, str]] = {}
        for name, digest in sorted(views.items()):
            matched = VIEW_NAME_RE.fullmatch(name)
            if matched is None or SHA256_RE.fullmatch(digest) is None:
                raise ValueError("snapshot_commit_invalid")
            view, segment = matched.groups()
            body = self._read_bounded(
                self._view_path(revision, view, segment), _MAX_DOCUMENT_BYTES
            )
            if hashlib.sha256(body).hexdigest() != digest:
                raise ValueError("snapshot_staged_view_missing")
            pointer_views[f"{view}.{segment}"] = {
                "key": f"snapshots/versions/{revision}/{view}/{segment}.json",
                "sha256": digest,
            }
        pointer = {
            "schemaVersion": POINTER_SCHEMA,
            "revision": revision,
            "publicationId": publication_id,
            "views": pointer_views,
        }
        encoded = json.dumps(
            pointer, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        with self._lock:
            current = self._pointer()
            current_revision = None if current is None else str(current["revision"])
            if current_revision == revision:
                if current["views"] != pointer_views:
                    raise ValueError("snapshot_revision_content_conflict")
                return {
                    "revision": revision,
                    "views": len(views),
                    "hashes": dict(sorted(views.items())),
                    "idempotent": True,
                }
            if current_revision != expected_current_revision:
                raise ValueError("snapshot_revision_conflict")
            self._atomic_write(self._pointer_path, encoded)
            readback = self._pointer()
            if readback != pointer:
                raise RuntimeError("snapshot_pointer_readback_mismatch")
        return {
            "revision": revision,
            "views": len(views),
            "hashes": dict(sorted(views.items())),
            "idempotent": False,
        }
