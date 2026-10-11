from __future__ import annotations

import hashlib
import io
import json
import unittest
from unittest.mock import MagicMock, patch

from economy_api.object_store import (
    IamObjectClient,
    MetadataIamTokenProvider,
    ObjectStorageEconomyStore,
)


class FakeClient:
    def __init__(self, payload: bytes, *, digest: str | None = None) -> None:
        revision = "r1"
        pointer = json.dumps({
            "schemaVersion": "aion2.economy.pointer/v1",
            "revision": revision,
            "views": {"market.default": {
                "key": f"snapshots/versions/{revision}/market/default.json",
                "sha256": digest or hashlib.sha256(payload).hexdigest(),
            }},
        }).encode()
        self.objects = {
            "snapshots/current.json": pointer,
            f"snapshots/versions/{revision}/market/default.json": payload,
        }
        self.calls: list[str] = []

    def get_object(self, *, Bucket: str, Key: str) -> dict[str, object]:
        self.calls.append(Key)
        if Bucket != "private-snapshots":
            raise AssertionError(Bucket)
        if Key not in self.objects:
            raise AssertionError(Key)
        payload = self.objects[Key]
        return {"Body": io.BytesIO(payload), "Metadata": {"sha256": hashlib.sha256(payload).hexdigest()}}


class ObjectStorageTests(unittest.TestCase):
    def test_metadata_token_uses_link_local_endpoint_and_required_header(self) -> None:
        response = io.BytesIO(b'{"access_token":"opaque-token","expires_in":300}')
        with patch(
            "economy_api.object_store.urllib.request.urlopen", return_value=response
        ) as mocked:
            provider = MetadataIamTokenProvider(clock=lambda: 10.0)
            self.assertEqual(provider(), "opaque-token")

        request = mocked.call_args.args[0]
        self.assertEqual(
            request.full_url,
            "http://169.254.169.254/computeMetadata/v1/"
            "instance/service-accounts/default/token",
        )
        self.assertEqual(request.get_header("Metadata-flavor"), "Google")

    def test_reads_hash_verified_current_snapshot_and_caches_it(self) -> None:
        payload = json.dumps({"revision": "r1", "items": []}).encode()
        client = FakeClient(payload)
        store = ObjectStorageEconomyStore(
            "private-snapshots", client=client, cache_seconds=30, clock=lambda: 10.0
        )
        self.assertEqual(store.read("market", "default")["revision"], "r1")  # type: ignore[index]
        self.assertEqual(store.read("market", "default")["revision"], "r1")  # type: ignore[index]
        self.assertEqual(client.calls, [
            "snapshots/current.json",
            "snapshots/versions/r1/market/default.json",
        ])

    def test_iam_client_uses_bearer_and_conditional_put(self) -> None:
        response = MagicMock()
        response.headers = {"ETag": '"etag-1"'}
        with patch(
            "economy_api.object_store.urllib.request.urlopen", return_value=response
        ) as mocked:
            result = IamObjectClient(token_provider=lambda: "opaque-iam-token").put_object(
                Bucket="private-snapshots",
                Key="snapshots/current.json",
                Body=b"{}",
                Metadata={"sha256": hashlib.sha256(b"{}").hexdigest()},
                IfMatch='"previous-etag"',
            )
        self.assertEqual(result["ETag"], '"etag-1"')
        req = mocked.call_args.args[0]
        self.assertEqual(req.get_method(), "PUT")
        self.assertEqual(req.get_header("Authorization"), "Bearer opaque-iam-token")
        self.assertEqual(req.get_header("If-match"), '"previous-etag"')
        self.assertIsNone(req.get_header("If-none-match"))

    def test_rejects_hash_mismatch_and_untrusted_key_parts(self) -> None:
        payload = b'{"revision":"r1"}'
        client = FakeClient(payload, digest="0" * 64)
        store = ObjectStorageEconomyStore("private-snapshots", client=client)
        with self.assertRaisesRegex(RuntimeError, "hash_mismatch"):
            store.read("market", "default")
        self.assertIsNone(store.read("../market", "default"))


if __name__ == "__main__":
    unittest.main()
