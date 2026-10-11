from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path

from economy_api.filesystem_store import FileSystemEconomyStore
from economy_api.snapshot_ingest import POINTER_SCHEMA, SNAPSHOT_SCHEMA, canonical_json


class FileSystemEconomyStoreTest(unittest.TestCase):
    def _view(self, revision: str, item_id: int = 1) -> bytes:
        return canonical_json(
            {
                "schemaVersion": SNAPSHOT_SCHEMA,
                "revision": revision,
                "generatedAt": "2026-10-11T00:00:00Z",
                "build": 25650019,
                "items": [{"itemId": item_id}],
            }
        )

    def test_atomic_commit_and_readback(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = FileSystemEconomyStore(directory)
            body = self._view("r1")
            digest = hashlib.sha256(body).hexdigest()
            store.stage_view("r1", "items.default.json", body, digest)
            result = store.commit_views(
                "r1", None, {"items.default.json": digest}, "1" * 32
            )
            self.assertFalse(result["idempotent"])
            self.assertEqual(store.current_revision(), "r1")
            self.assertEqual(store.read("items", "default")["items"][0]["itemId"], 1)

            pointer = json.loads((Path(directory) / "current.json").read_bytes())
            self.assertEqual(pointer["schemaVersion"], POINTER_SCHEMA)
            self.assertEqual(pointer["publicationId"], "1" * 32)

    def test_revision_switch_is_cas_and_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = FileSystemEconomyStore(directory)
            first = self._view("r1")
            first_digest = hashlib.sha256(first).hexdigest()
            store.stage_view("r1", "items.default.json", first, first_digest)
            store.commit_views("r1", None, {"items.default.json": first_digest}, "1" * 32)

            second = self._view("r2", item_id=2)
            second_digest = hashlib.sha256(second).hexdigest()
            store.stage_view("r2", "items.default.json", second, second_digest)
            with self.assertRaisesRegex(ValueError, "snapshot_revision_conflict"):
                store.commit_views(
                    "r2", None, {"items.default.json": second_digest}, "2" * 32
                )
            result = store.commit_views(
                "r2", "r1", {"items.default.json": second_digest}, "2" * 32
            )
            self.assertFalse(result["idempotent"])
            repeated = store.commit_views(
                "r2", "r1", {"items.default.json": second_digest}, "2" * 32
            )
            self.assertTrue(repeated["idempotent"])
            self.assertEqual(store.read("items", "default")["items"][0]["itemId"], 2)

    def test_tampered_file_fails_readback(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = FileSystemEconomyStore(directory)
            body = self._view("r1")
            digest = hashlib.sha256(body).hexdigest()
            store.stage_view("r1", "items.default.json", body, digest)
            store.commit_views("r1", None, {"items.default.json": digest}, "1" * 32)
            target = Path(directory) / "versions" / "r1" / "items" / "default.json"
            target.write_bytes(b"{}")
            with self.assertRaisesRegex(RuntimeError, "economy_pointer_hash_mismatch"):
                store.read("items", "default")

    def test_crawler_status_is_revision_bound_and_read_back(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = FileSystemEconomyStore(directory)
            revision = "b25650019-cloudru-test"
            body = self._view(revision)
            view_digest = hashlib.sha256(body).hexdigest()
            store.stage_view(revision, "items.default.json", body, view_digest)
            store.commit_views(
                revision, None, {"items.default.json": view_digest}, "1" * 32
            )
            status = {
                "schema_version": "aion2.market-autorefresh.status/v2",
                "observed_at_utc": "2026-10-11T00:00:00Z",
                "scan_observed_at_utc": "2026-10-11T00:00:00Z",
                "state": "verifying",
                "trigger": "scheduled",
                "run_id": "market-" + "a" * 32,
                "revision": revision,
                "release": "b" * 40,
                "server_count": 100,
                "request_count": 100,
                "summary_rows": 1,
                "snapshot_changed": True,
                "packet_bus_segments": 1,
            }
            encoded = canonical_json(status)
            digest = hashlib.sha256(encoded).hexdigest()
            receipt = store.write_crawler_status(
                revision,
                encoded,
                digest,
                now=datetime(2026, 10, 11, tzinfo=UTC),
            )
            self.assertFalse(receipt["idempotent"])
            self.assertEqual(
                store.read_crawler_status(datetime(2026, 10, 11, tzinfo=UTC))[
                    "economyRevision"
                ],
                revision,
            )
            repeated = store.write_crawler_status(
                revision,
                encoded,
                digest,
                now=datetime(2026, 10, 11, tzinfo=UTC),
            )
            self.assertTrue(repeated["idempotent"])


if __name__ == "__main__":
    unittest.main()
