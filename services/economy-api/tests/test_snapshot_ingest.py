from __future__ import annotations

import base64
import gzip
import hashlib
import io
import json
import unittest
from datetime import UTC, datetime, timedelta
from unittest.mock import patch

from economy_api import yandex_handler
from economy_api.app import ApiApplication, AppConfig
from economy_api.memory_repository import MemoryRepository
from economy_api.models import User
from economy_api.object_store import ObjectStorageEconomyStore, _ObjectConflict
from economy_api.security import create_desktop_sync_token, hash_password
from economy_api.snapshot_ingest import (
    COMMIT_SCHEMA,
    REQUIRED_DEFAULT_VIEWS,
    UPLOAD_SCHEMA,
    canonical_json,
    commit_payload_digest,
    sha256_bytes,
    view_upload_idempotency_key,
)
from economy_api.store import MemoryEconomyStore


NOW = datetime(2026, 10, 9, 8, 0, tzinfo=UTC)
SECRET = b"snapshot-ingest-test-secret-at-least-32-bytes"


def api_event(
    path: str,
    payload: dict[str, object] | None,
    *,
    token: str | None,
    idempotency_key: str | None = None,
    cookie: str | None = None,
) -> dict[str, object]:
    headers = {"Content-Type": "application/json", "Content-Encoding": "gzip"}
    if token is not None:
        headers["Authorization"] = "Bearer " + token
    if idempotency_key is not None:
        headers["Idempotency-Key"] = idempotency_key
    if cookie is not None:
        headers["Cookie"] = cookie
    raw = gzip.compress(canonical_json(payload or {}), mtime=0)
    return {
        "httpMethod": "POST",
        "path": path,
        "headers": headers,
        "body": base64.b64encode(raw).decode("ascii"),
        "isBase64Encoded": True,
        "requestContext": {
            "requestId": "snapshot-test",
            "identity": {"sourceIp": "203.0.113.9"},
        },
    }


def readback_event(*, token: str | None, cookie: str | None = None) -> dict[str, object]:
    headers: dict[str, str] = {}
    if token is not None:
        headers["Authorization"] = "Bearer " + token
    if cookie is not None:
        headers["Cookie"] = cookie
    return {
        "httpMethod": "GET",
        "path": "/api/v1/crawler/economy/readback",
        "headers": headers,
        "requestContext": {
            "requestId": "snapshot-readback-test",
            "identity": {"sourceIp": "203.0.113.9"},
        },
    }


def response_body(response: dict[str, object]) -> dict[str, object]:
    return json.loads(str(response.get("body") or "{}"))


def view_document(name: str, revision: str, *, sensitive: bool = False) -> dict[str, object]:
    prefix = name.split(".", 1)[0]
    document: dict[str, object] = {
        "schemaVersion": "aion2.economy.snapshot/v1",
        "revision": revision,
        "generatedAt": "2026-10-09T08:00:00Z",
    }
    field = {
        "market": "items",
        "items": "items",
        "servers": "servers",
        "craft": "recipes",
        "farm-value": "farmSessions",
    }.get(prefix)
    if field is not None:
        document[field] = []
    else:
        document.update({key: [] for key in ("up", "down", "new", "liquid", "listings")})
    if sensitive:
        document["access_token"] = "must-not-pass"
    return document


class SnapshotIngestApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.repo = MemoryRepository()
        self.repo.create_user(
            User(
                username="rootadmin",
                user_id="admin-id",
                password_hash=hash_password("correct horse battery staple", salt=b"0123456789abcdef"),
                role="admin",
                created_at=NOW,
                updated_at=NOW,
            )
        )
        issued = create_desktop_sync_token(
            "rootadmin",
            SECRET,
            now=NOW,
            ttl=timedelta(hours=1),
            label="primary",
            scope="crawler:control",
        )
        self.repo.create_desktop_sync_token(issued.record)
        self.token = issued.token_value
        self.store = MemoryEconomyStore()
        self.app = ApiApplication(
            self.repo,
            self.store,
            SECRET,
            AppConfig(),
            clock=lambda: NOW,
        )

    def upload(self, name: str, revision: str, *, sensitive: bool = False) -> dict[str, object]:
        document = view_document(name, revision, sensitive=sensitive)
        digest = sha256_bytes(canonical_json(document))
        payload = {
            "schema": UPLOAD_SCHEMA,
            "revision": revision,
            "name": name,
            "sha256": digest,
            "document": document,
        }
        return self.app.handle(
            api_event(
                "/api/v1/crawler/economy/views",
                payload,
                token=self.token,
                idempotency_key=view_upload_idempotency_key(revision, name, digest),
            )
        )

    def stage_defaults(self, revision: str) -> dict[str, str]:
        hashes: dict[str, str] = {}
        for name in sorted(REQUIRED_DEFAULT_VIEWS):
            document = view_document(name, revision)
            digest = sha256_bytes(canonical_json(document))
            response = self.upload(name, revision)
            self.assertEqual(response["statusCode"], 200, response)
            hashes[name] = digest
        return hashes

    def commit(
        self, revision: str, expected: str | None, hashes: dict[str, str]
    ) -> dict[str, object]:
        payload = {
            "schema": COMMIT_SCHEMA,
            "revision": revision,
            "expectedCurrentRevision": expected,
            "views": hashes,
        }
        return self.app.handle(
            api_event(
                "/api/v1/crawler/economy/commit",
                payload,
                token=self.token,
                idempotency_key=commit_payload_digest(payload),
            )
        )

    def test_requires_crawler_bearer_and_rejects_cookie_fallback(self) -> None:
        name = "items.default.json"
        document = view_document(name, "r1")
        digest = sha256_bytes(canonical_json(document))
        payload = {
            "schema": UPLOAD_SCHEMA,
            "revision": "r1",
            "name": name,
            "sha256": digest,
            "document": document,
        }
        key = view_upload_idempotency_key("r1", name, digest)
        missing = self.app.handle(
            api_event(
                "/api/v1/crawler/economy/views", payload, token=None, idempotency_key=key
            )
        )
        cookie = self.app.handle(
            api_event(
                "/api/v1/crawler/economy/views",
                payload,
                token=self.token,
                idempotency_key=key,
                cookie="session=forbidden",
            )
        )
        self.assertEqual(missing["statusCode"], 401)
        self.assertEqual(cookie["statusCode"], 401)

    def test_crawler_readback_is_bearer_only_and_reports_deployed_release(self) -> None:
        self.app = ApiApplication(
            self.repo,
            self.store,
            SECRET,
            AppConfig(api_release_sha256="d" * 64),
            clock=lambda: NOW,
        )
        missing = self.app.handle(readback_event(token=None))
        cookie = self.app.handle(readback_event(token=self.token, cookie="forbidden=1"))
        accepted = self.app.handle(readback_event(token=self.token))

        self.assertEqual(missing["statusCode"], 401)
        self.assertEqual(cookie["statusCode"], 401)
        self.assertEqual(accepted["statusCode"], 200)
        self.assertEqual(
            response_body(accepted),
            {
                "schema": "aion2.economy.crawler-readback/v1",
                "currentRevision": None,
                "crawlerStatus": None,
                "apiRelease": "d" * 64,
            },
        )

    def test_rejects_schema_hash_sensitive_fields_and_oversize_wire(self) -> None:
        sensitive = self.upload("items.default.json", "r1", sensitive=True)
        self.assertEqual(sensitive["statusCode"], 400)
        self.assertEqual(
            response_body(sensitive)["error"], "snapshot_sensitive_field_rejected"
        )

        document = view_document("items.default.json", "r1")
        invalid = {
            "schema": "wrong",
            "revision": "r1",
            "name": "items.default.json",
            "sha256": sha256_bytes(canonical_json(document)),
            "document": document,
        }
        bad_schema = self.app.handle(
            api_event(
                "/api/v1/crawler/economy/views",
                invalid,
                token=self.token,
                idempotency_key="0" * 64,
            )
        )
        self.assertEqual(bad_schema["statusCode"], 400)

        oversized = {
            "httpMethod": "POST",
            "path": "/api/v1/crawler/economy/views",
            "headers": {"Authorization": "Bearer " + self.token},
            "body": "x" * (1024 * 1024 + 1),
            "requestContext": {"identity": {"sourceIp": "203.0.113.10"}},
        }
        too_large = self.app.handle(oversized)
        self.assertEqual(too_large["statusCode"], 413)

    def test_rejects_raw_or_path_extras_even_when_view_shape_is_otherwise_valid(self) -> None:
        for field in ("payload_hex", "source_path", "accessToken"):
            with self.subTest(field=field):
                name = "items.default.json"
                document = view_document(name, "r1")
                document[field] = "unsafe"
                digest = sha256_bytes(canonical_json(document))
                response = self.app.handle(
                    api_event(
                        "/api/v1/crawler/economy/views",
                        {
                            "schema": UPLOAD_SCHEMA,
                            "revision": "r1",
                            "name": name,
                            "sha256": digest,
                            "document": document,
                        },
                        token=self.token,
                        idempotency_key=view_upload_idempotency_key("r1", name, digest),
                    )
                )
                self.assertEqual(response["statusCode"], 400)
                self.assertIn(
                    response_body(response)["error"],
                    {"snapshot_view_shape_invalid", "snapshot_sensitive_field_rejected"},
                )

    def test_rejects_unknown_nested_raw_request_and_path_fields(self) -> None:
        for field in ("raw", "requestBody", "filePath", "path"):
            with self.subTest(field=field):
                name = "items.default.json"
                document = view_document(name, "r1")
                document["items"] = [{field: "PRIVATE"}]
                digest = sha256_bytes(canonical_json(document))
                response = self.app.handle(
                    api_event(
                        "/api/v1/crawler/economy/views",
                        {
                            "schema": UPLOAD_SCHEMA,
                            "revision": "r1",
                            "name": name,
                            "sha256": digest,
                            "document": document,
                        },
                        token=self.token,
                        idempotency_key=view_upload_idempotency_key("r1", name, digest),
                    )
                )
                self.assertEqual(response["statusCode"], 400)
                self.assertEqual(
                    response_body(response)["error"],
                    "snapshot_sensitive_field_rejected",
                )

    def test_revision_cas_idempotent_replay_and_verified_handler_receipt(self) -> None:
        hashes = self.stage_defaults("r1")
        first = self.commit("r1", None, hashes)
        self.assertEqual(first["statusCode"], 200, first)
        self.assertEqual(response_body(first)["status"], "published")
        self.assertEqual(self.store.current_revision(), "r1")

        replay = self.commit("r1", None, hashes)
        self.assertEqual(replay["statusCode"], 200, replay)
        self.assertEqual(response_body(replay)["status"], "idempotent")
        replay_with_current = self.commit("r1", "r1", hashes)
        self.assertEqual(replay_with_current["statusCode"], 200, replay_with_current)
        self.assertEqual(response_body(replay_with_current)["status"], "idempotent")

        second_hashes = self.stage_defaults("r2")
        stale = self.commit("r2", None, second_hashes)
        self.assertEqual(stale["statusCode"], 409, stale)
        self.assertEqual(response_body(stale)["error"], "snapshot_revision_conflict")

        # Exercise the deployed entrypoint shape, not only ApiApplication directly.
        with patch.object(yandex_handler, "_application", self.app):
            handler_replay = yandex_handler.handler(
                api_event(
                    "/api/v1/crawler/economy/commit",
                    {
                        "schema": COMMIT_SCHEMA,
                        "revision": "r1",
                        "expectedCurrentRevision": None,
                        "views": hashes,
                    },
                    token=self.token,
                    idempotency_key=commit_payload_digest(
                        {
                            "schema": COMMIT_SCHEMA,
                            "revision": "r1",
                            "expectedCurrentRevision": None,
                            "views": hashes,
                        }
                    ),
                ),
                object(),
            )
        self.assertEqual(handler_replay["statusCode"], 200, handler_replay)
        self.assertTrue(response_body(handler_replay)["verified"])


class FakeWritableObjectClient:
    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}
        self.etags: dict[str, str] = {}

    def get_object(self, *, Bucket: str, Key: str) -> dict[str, object]:
        if Key not in self.objects:
            from economy_api.object_store import _ObjectNotFound

            raise _ObjectNotFound
        body = self.objects[Key]
        return {
            "Body": io.BytesIO(body),
            "Metadata": {"sha256": hashlib.sha256(body).hexdigest()},
            "ETag": self.etags[Key],
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
    ) -> dict[str, object]:
        if IfNoneMatch == "*" and Key in self.objects:
            raise _ObjectConflict
        if IfMatch is not None and self.etags.get(Key) != IfMatch:
            raise _ObjectConflict
        self.objects[Key] = bytes(Body)
        etag = '"' + hashlib.md5(Body, usedforsecurity=False).hexdigest() + '"'
        self.etags[Key] = etag
        return {"ETag": etag}


class ObjectStoragePublicationTests(unittest.TestCase):
    def test_immutable_stage_cas_commit_and_object_readback(self) -> None:
        client = FakeWritableObjectClient()
        store = ObjectStorageEconomyStore("private", client=client, cache_seconds=0)
        hashes: dict[str, str] = {}
        for name in sorted(REQUIRED_DEFAULT_VIEWS):
            encoded = canonical_json(view_document(name, "r1"))
            digest = sha256_bytes(encoded)
            store.stage_view("r1", name, encoded, digest)
            hashes[name] = digest
        receipt = store.commit_views("r1", None, hashes, "1" * 32)
        self.assertFalse(receipt["idempotent"])
        self.assertEqual(store.current_revision(), "r1")
        self.assertEqual(store.read("items", "default")["revision"], "r1")  # type: ignore[index]

        replay = store.commit_views("r1", None, hashes, "1" * 32)
        self.assertTrue(replay["idempotent"])
        r2_hashes: dict[str, str] = {}
        for name in sorted(REQUIRED_DEFAULT_VIEWS):
            encoded = canonical_json(view_document(name, "r2"))
            digest = sha256_bytes(encoded)
            store.stage_view("r2", name, encoded, digest)
            r2_hashes[name] = digest
        with self.assertRaisesRegex(ValueError, "revision_conflict"):
            store.commit_views("r2", None, r2_hashes, "2" * 32)


if __name__ == "__main__":
    unittest.main()
