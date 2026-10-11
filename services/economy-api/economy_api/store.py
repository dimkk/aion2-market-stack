from __future__ import annotations

import copy
from typing import Any, Protocol


class EconomyStore(Protocol):
    def read(self, view: str, segment: str) -> dict[str, Any] | None: ...

    def current_revision(self) -> str | None: ...

    def stage_view(
        self, revision: str, name: str, encoded: bytes, digest: str
    ) -> None: ...

    def commit_views(
        self,
        revision: str,
        expected_current_revision: str | None,
        views: dict[str, str],
        publication_id: str,
    ) -> dict[str, Any]: ...


class MemoryEconomyStore:
    def __init__(self, data: dict[tuple[str, str], dict[str, Any]] | None = None) -> None:
        self.data = data or {}
        self.staged: dict[tuple[str, str], bytes] = {}
        self.revision: str | None = None
        self.publication_id: str | None = None

    def read(self, view: str, segment: str) -> dict[str, Any] | None:
        value = self.data.get((view, segment))
        return copy.deepcopy(value) if value is not None else None

    def current_revision(self) -> str | None:
        return self.revision

    def stage_view(
        self, revision: str, name: str, encoded: bytes, digest: str
    ) -> None:
        import hashlib

        if hashlib.sha256(encoded).hexdigest() != digest:
            raise ValueError("snapshot_view_hash_mismatch")
        key = (revision, name)
        current = self.staged.get(key)
        if current is not None and current != encoded:
            raise ValueError("snapshot_immutable_conflict")
        self.staged[key] = bytes(encoded)

    def commit_views(
        self,
        revision: str,
        expected_current_revision: str | None,
        views: dict[str, str],
        publication_id: str,
    ) -> dict[str, Any]:
        import hashlib
        import json

        if self.revision == revision:
            for name, digest in views.items():
                encoded = self.staged.get((revision, name))
                if encoded is None or hashlib.sha256(encoded).hexdigest() != digest:
                    raise ValueError("snapshot_revision_content_conflict")
            return {
                "revision": revision,
                "views": len(views),
                "hashes": dict(sorted(views.items())),
                "idempotent": True,
            }
        if self.revision != expected_current_revision:
            raise ValueError("snapshot_revision_conflict")
        for name, digest in views.items():
            encoded = self.staged.get((revision, name))
            if encoded is None or hashlib.sha256(encoded).hexdigest() != digest:
                raise ValueError("snapshot_staged_view_missing")
        for name in views:
            view, segment, _json = name.split(".", 2)
            self.data[(view, segment)] = json.loads(self.staged[(revision, name)])
        self.revision = revision
        self.publication_id = publication_id
        return {
            "revision": revision,
            "views": len(views),
            "hashes": dict(sorted(views.items())),
            "idempotent": False,
        }
