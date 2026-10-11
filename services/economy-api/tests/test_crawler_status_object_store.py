from __future__ import annotations

import hashlib
import io
import json
import unittest
from datetime import UTC, datetime

from economy_api.object_store import ObjectStorageEconomyStore, _ObjectNotFound


REVISION = "b25650019-20261009T120000Z"
RELEASE = "b" * 40


class FakeClient:
    def __init__(self, objects: dict[str, bytes]) -> None:
        self.objects = objects

    def get_object(self, *, Bucket: str, Key: str) -> dict[str, object]:
        del Bucket
        if Key not in self.objects:
            raise _ObjectNotFound
        body = self.objects[Key]
        return {
            "Body": io.BytesIO(body),
            "Metadata": {"sha256": hashlib.sha256(body).hexdigest()},
        }


def object_set() -> dict[str, bytes]:
    market_key = f"snapshots/versions/{REVISION}/market/default.json"
    market = json.dumps({"revision": REVISION}).encode()
    pointer = json.dumps(
        {
            "schemaVersion": "aion2.economy.pointer/v1",
            "revision": REVISION,
            "views": {
                "market.default": {
                    "key": market_key,
                    "sha256": hashlib.sha256(market).hexdigest(),
                }
            },
        }
    ).encode()
    last_success = {
        "observed_at_utc": "2026-10-09T12:00:00Z",
        "revision": REVISION,
        "release": RELEASE,
        "server_count": 100,
        "request_count": 100,
        "summary_rows": 123,
        "snapshot_changed": True,
    }
    status = json.dumps(
        {
            "schema_version": "aion2.market-autorefresh.status/v1",
            "observed_at_utc": "2026-10-09T12:00:00Z",
            "state": "published",
            "trigger": "scheduled",
            "revision": REVISION,
            "release": RELEASE,
            "server_count": 100,
            "request_count": 100,
            "summary_rows": 123,
            "snapshot_changed": True,
            "packet_bus_segments": 1,
            "last_success": last_success,
        }
    ).encode()
    return {
        "snapshots/current.json": pointer,
        market_key: market,
        f"snapshots/crawler-status/{REVISION}/current.json": status,
    }


class CrawlerStatusObjectStoreTests(unittest.TestCase):
    def test_reads_status_only_through_current_pointer_revision(self) -> None:
        store = ObjectStorageEconomyStore("bucket", client=FakeClient(object_set()))

        status = store.read_crawler_status(datetime(2026, 10, 9, 12, 30, tzinfo=UTC))

        self.assertIsNotNone(status)
        self.assertEqual(REVISION, status["economyRevision"])
        self.assertEqual("standalone_object", status["source"])

    def test_missing_status_is_explicit_publish_failure(self) -> None:
        objects = object_set()
        del objects[f"snapshots/crawler-status/{REVISION}/current.json"]
        store = ObjectStorageEconomyStore("bucket", client=FakeClient(objects))

        status = store.read_crawler_status(
            datetime(2026, 10, 9, 12, 30, tzinfo=UTC)
        )
        self.assertEqual("failed", status["status"])
        self.assertEqual(
            "crawler_status_missing_current_revision", status["errors"][0]["code"]
        )

    def test_new_pointer_with_previous_status_is_sanitized_degraded(self) -> None:
        objects = object_set()
        next_revision = "b25650019-20261009T123000Z"
        next_market_key = f"snapshots/versions/{next_revision}/market/default.json"
        next_market = json.dumps({"revision": next_revision}).encode()
        pointer = json.loads(objects["snapshots/current.json"])
        pointer["revision"] = next_revision
        pointer["views"]["market.default"] = {
            "key": next_market_key,
            "sha256": hashlib.sha256(next_market).hexdigest(),
        }
        objects["snapshots/current.json"] = json.dumps(pointer).encode()
        objects[next_market_key] = next_market
        store = ObjectStorageEconomyStore("bucket", client=FakeClient(objects))

        status = store.read_crawler_status(
            datetime(2026, 10, 9, 12, 30, tzinfo=UTC)
        )

        self.assertEqual("failed", status["status"])
        self.assertEqual(next_revision, status["economyRevision"])
        self.assertEqual(
            "crawler_status_missing_current_revision", status["errors"][0]["code"]
        )


if __name__ == "__main__":
    unittest.main()
