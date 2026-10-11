from __future__ import annotations

from datetime import UTC, datetime
import json
import unittest

from economy_api.legacy_migration import (
    MigrationError,
    export_ydb_bundle,
    import_postgres_bundle,
    validate_bundle,
)
from economy_api.security import DUMMY_PASSWORD_HASH


class FakeYdb:
    def execute(self, query: str, _params: dict[str, object]) -> list[dict[str, object]]:
        now = datetime(2026, 10, 11, tzinfo=UTC)
        if "FROM users" in query:
            return [{
                "username": "owner", "user_id": "u1",
                "password_hash": DUMMY_PASSWORD_HASH,
                "role": "admin", "created_at": now, "updated_at": now,
            }]
        if "FROM farm_sessions" in query:
            return [{
                "owner_username": "owner", "session_id": "s1",
                "client_session_id": "c1", "payload_json": '{"kina":12}',
                "created_at": now, "updated_at": now,
            }]
        if "FROM audit_events" in query:
            return [{
                "event_id": "e1", "occurred_at": now, "kind": "login",
                "actor_user_id": "u1", "target_user_id": None,
                "request_id": "r1", "outcome": "ok",
            }]
        raise AssertionError(query)


class _Context:
    def __init__(self, value):
        self.value = value

    def __enter__(self):
        return self.value

    def __exit__(self, *_args):
        return False


class FakeCursor:
    def __init__(self, *, existing=None, target_counts=None):
        self.existing = existing
        self.target_counts = target_counts or {
            "users_count": 0,
            "farm_sessions_count": 0,
            "audit_events_count": 0,
        }
        self.last = ""
        self.executed_many: list[tuple[str, int]] = []
        self.receipt_inserted = False

    def execute(self, statement, _params=None):
        self.last = statement
        if "INSERT INTO migration_receipts" in statement:
            self.receipt_inserted = True

    def executemany(self, statement, rows):
        self.executed_many.append((statement, len(rows)))

    def fetchone(self):
        if "FROM migration_receipts" in self.last:
            return self.existing
        if "SELECT count(*) FROM users" in self.last:
            return self.target_counts
        raise AssertionError(self.last)


class FakeDatabase:
    def __init__(self, cursor):
        self._cursor = cursor

    def transaction(self):
        return _Context(None)

    def cursor(self):
        return _Context(self._cursor)


class FakePostgres:
    def __init__(self, cursor):
        self.pool = type("Pool", (), {"connection": lambda _self: _Context(FakeDatabase(cursor))})()


class LegacyMigrationTests(unittest.TestCase):
    def test_export_validates_without_sessions_or_tokens(self) -> None:
        encoded = export_ydb_bundle(FakeYdb())
        payload = validate_bundle(encoded)
        self.assertEqual(payload["users"][0]["username"], "owner")
        self.assertEqual(payload["farm_sessions"][0]["payload"], {"kina": 12})
        self.assertNotIn("sessions", payload)
        self.assertNotIn("desktop_sync_tokens", payload)

    def test_modified_payload_is_rejected(self) -> None:
        value = json.loads(export_ydb_bundle(FakeYdb()))
        value["payload"]["users"][0]["role"] = "admin2"
        with self.assertRaisesRegex(MigrationError, "integrity"):
            validate_bundle(json.dumps(value).encode())

    def test_invalid_password_hash_is_rejected_with_valid_digest(self) -> None:
        value = json.loads(export_ydb_bundle(FakeYdb()))
        value["payload"]["users"][0]["password_hash"] = (
            "$argon2id$v=19$m=65536,t=3,p=4$c2FsdA$YWJj"
        )
        from economy_api.legacy_migration import _canonical
        import hashlib
        value["sha256"] = hashlib.sha256(_canonical(value["payload"])).hexdigest()
        with self.assertRaisesRegex(MigrationError, "legacy_user_invalid"):
            validate_bundle(_canonical(value))

    def test_postgres_import_is_atomic_and_receipted(self) -> None:
        encoded = export_ydb_bundle(FakeYdb())
        cursor = FakeCursor()
        receipt = import_postgres_bundle(FakePostgres(cursor), encoded)
        self.assertFalse(receipt["replayed"])
        self.assertEqual(1, receipt["users"])
        self.assertEqual([1, 1, 1], [count for _statement, count in cursor.executed_many])
        self.assertTrue(cursor.receipt_inserted)

    def test_postgres_exact_receipt_replays_without_writes(self) -> None:
        import hashlib

        encoded = export_ydb_bundle(FakeYdb())
        cursor = FakeCursor(existing={
            "source_sha256": hashlib.sha256(encoded).hexdigest(),
            "users_count": 1,
            "farm_sessions_count": 1,
            "audit_events_count": 1,
        })
        receipt = import_postgres_bundle(FakePostgres(cursor), encoded)
        self.assertTrue(receipt["replayed"])
        self.assertEqual([], cursor.executed_many)

    def test_postgres_conflicting_receipt_is_rejected(self) -> None:
        encoded = export_ydb_bundle(FakeYdb())
        cursor = FakeCursor(existing={
            "source_sha256": "0" * 64,
            "users_count": 1,
            "farm_sessions_count": 1,
            "audit_events_count": 1,
        })
        with self.assertRaisesRegex(MigrationError, "receipt_conflict"):
            import_postgres_bundle(FakePostgres(cursor), encoded)

    def test_postgres_nonempty_target_is_rejected(self) -> None:
        encoded = export_ydb_bundle(FakeYdb())
        cursor = FakeCursor(target_counts={
            "users_count": 1,
            "farm_sessions_count": 0,
            "audit_events_count": 0,
        })
        with self.assertRaisesRegex(MigrationError, "target_not_empty"):
            import_postgres_bundle(FakePostgres(cursor), encoded)


if __name__ == "__main__":
    unittest.main()
