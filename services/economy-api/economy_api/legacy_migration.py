from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import re
import sys
from datetime import UTC, datetime
from typing import Any

from .postgres_repository import PostgresConnection
from .ydb_repository import YdbConnection, _timestamp


SCHEMA = "aion2.economy-private-state-migration/v1"
MIGRATION_ID = "yandex-ydb-private-state-v1"
MAX_BUNDLE_BYTES = 64 * 1024 * 1024
MAX_USERS = 1_000
MAX_FARM_SESSIONS = 50_000
MAX_AUDIT_EVENTS = 100_000
_ROLE = frozenset({"registered", "approved", "admin"})
_SCRYPT_PROFILES = frozenset({(1 << 17, 8, 1), (1 << 14, 8, 1)})


class MigrationError(ValueError):
    pass


def _valid_password_hash(value: object) -> bool:
    try:
        if not isinstance(value, str) or len(value) > 512:
            return False
        kind, n, r, p, salt, expected = value.split("$", 5)
        if kind != "scrypt" or (int(n), int(r), int(p)) not in _SCRYPT_PROFILES:
            return False
        decoded_salt = base64.urlsafe_b64decode(salt + "=" * (-len(salt) % 4))
        decoded_expected = base64.urlsafe_b64decode(
            expected + "=" * (-len(expected) % 4)
        )
        return len(decoded_salt) == 16 and len(decoded_expected) == 32
    except (ValueError, TypeError):
        return False


def _stamp(value: object) -> str:
    return _timestamp(value).astimezone(UTC).isoformat().replace("+00:00", "Z")


def _canonical(value: object) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def _payload(connection: Any) -> dict[str, Any]:
    users = [
        {
            "username": str(row["username"]),
            "user_id": str(row["user_id"]),
            "password_hash": str(row["password_hash"]),
            "role": str(row["role"]),
            "created_at": _stamp(row["created_at"]),
            "updated_at": _stamp(row["updated_at"]),
        }
        for row in connection.execute(
            """SELECT username,user_id,password_hash,role,created_at,updated_at
               FROM users ORDER BY username LIMIT 1001;""",
            {},
        )
    ]
    farms: list[dict[str, Any]] = []
    for row in connection.execute(
        """SELECT owner_username,session_id,client_session_id,payload_json,
                  created_at,updated_at
           FROM farm_sessions ORDER BY owner_username,session_id LIMIT 50001;""",
        {},
    ):
        body = row["payload_json"]
        if isinstance(body, str):
            body = json.loads(body)
        if not isinstance(body, dict):
            raise MigrationError("legacy_farm_payload_invalid")
        farms.append(
            {
                "owner_username": str(row["owner_username"]),
                "session_id": str(row["session_id"]),
                "client_session_id": str(row["client_session_id"]),
                "payload": body,
                "created_at": _stamp(row["created_at"]),
                "updated_at": _stamp(row["updated_at"]),
            }
        )
    audits = [
        {
            "event_id": str(row["event_id"]),
            "occurred_at": _stamp(row["occurred_at"]),
            "kind": str(row["kind"]),
            "actor_user_id": None
            if row["actor_user_id"] is None
            else str(row["actor_user_id"]),
            "target_user_id": None
            if row["target_user_id"] is None
            else str(row["target_user_id"]),
            "request_id": str(row["request_id"]),
            "outcome": str(row["outcome"]),
        }
        for row in connection.execute(
            """SELECT event_id,occurred_at,kind,actor_user_id,target_user_id,
                      request_id,outcome
               FROM audit_events ORDER BY occurred_at,event_id LIMIT 100001;""",
            {},
        )
    ]
    if len(users) > MAX_USERS or len(farms) > MAX_FARM_SESSIONS or len(audits) > MAX_AUDIT_EVENTS:
        raise MigrationError("legacy_state_limit_exceeded")
    return {"users": users, "farm_sessions": farms, "audit_events": audits}


def export_ydb_bundle(connection: Any) -> bytes:
    payload = _payload(connection)
    encoded_payload = _canonical(payload)
    envelope = {
        "schema": SCHEMA,
        "sha256": hashlib.sha256(encoded_payload).hexdigest(),
        "payload": payload,
    }
    encoded = _canonical(envelope)
    if len(encoded) > MAX_BUNDLE_BYTES:
        raise MigrationError("legacy_state_limit_exceeded")
    return encoded


def _timestamp_iso(value: object) -> datetime:
    if not isinstance(value, str) or len(value) > 64:
        raise MigrationError("legacy_timestamp_invalid")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise MigrationError("legacy_timestamp_invalid") from exc
    if parsed.tzinfo is None:
        raise MigrationError("legacy_timestamp_invalid")
    return parsed.astimezone(UTC)


def validate_bundle(encoded: bytes) -> dict[str, list[dict[str, Any]]]:
    if not encoded or len(encoded) > MAX_BUNDLE_BYTES:
        raise MigrationError("legacy_bundle_size_invalid")
    try:
        envelope = json.loads(encoded)
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError) as exc:
        raise MigrationError("legacy_bundle_json_invalid") from exc
    if (
        not isinstance(envelope, dict)
        or set(envelope) != {"schema", "sha256", "payload"}
        or envelope.get("schema") != SCHEMA
        or not isinstance(envelope.get("payload"), dict)
        or not isinstance(envelope.get("sha256"), str)
        or hashlib.sha256(_canonical(envelope["payload"])).hexdigest()
        != envelope["sha256"]
    ):
        raise MigrationError("legacy_bundle_integrity_invalid")
    payload = envelope["payload"]
    if set(payload) != {"users", "farm_sessions", "audit_events"} or any(
        not isinstance(payload.get(name), list)
        for name in ("users", "farm_sessions", "audit_events")
    ):
        raise MigrationError("legacy_bundle_shape_invalid")
    users = payload["users"]
    farms = payload["farm_sessions"]
    audits = payload["audit_events"]
    if len(users) > MAX_USERS or len(farms) > MAX_FARM_SESSIONS or len(audits) > MAX_AUDIT_EVENTS:
        raise MigrationError("legacy_state_limit_exceeded")
    usernames: set[str] = set()
    user_ids: set[str] = set()
    for row in users:
        if not isinstance(row, dict) or set(row) != {
            "username", "user_id", "password_hash", "role", "created_at", "updated_at"
        }:
            raise MigrationError("legacy_user_invalid")
        username = row["username"]
        user_id = row["user_id"]
        if (
            not isinstance(username, str)
            or not re.fullmatch(r"[a-z0-9][a-z0-9._-]{2,63}", username)
            or not isinstance(user_id, str)
            or len(user_id) > 128
            or row["role"] not in _ROLE
            or not _valid_password_hash(row["password_hash"])
            or username in usernames
            or user_id in user_ids
        ):
            raise MigrationError("legacy_user_invalid")
        _timestamp_iso(row["created_at"])
        _timestamp_iso(row["updated_at"])
        usernames.add(username)
        user_ids.add(user_id)
    for row in farms:
        if (
            not isinstance(row, dict)
            or set(row) != {
                "owner_username", "session_id", "client_session_id", "payload",
                "created_at", "updated_at",
            }
            or row["owner_username"] not in usernames
            or not isinstance(row["session_id"], str)
            or not isinstance(row["client_session_id"], str)
            or not isinstance(row["payload"], dict)
        ):
            raise MigrationError("legacy_farm_invalid")
        _timestamp_iso(row["created_at"])
        _timestamp_iso(row["updated_at"])
    for row in audits:
        if (
            not isinstance(row, dict)
            or set(row) != {
                "event_id", "occurred_at", "kind", "actor_user_id", "target_user_id",
                "request_id", "outcome",
            }
            or not all(
                isinstance(row[name], str)
                for name in ("event_id", "kind", "request_id", "outcome")
            )
            or row["actor_user_id"] is not None
            and not isinstance(row["actor_user_id"], str)
            or row["target_user_id"] is not None
            and not isinstance(row["target_user_id"], str)
        ):
            raise MigrationError("legacy_audit_invalid")
        _timestamp_iso(row["occurred_at"])
    return payload


def import_postgres_bundle(connection: PostgresConnection, encoded: bytes) -> dict[str, Any]:
    payload = validate_bundle(encoded)
    source_sha256 = hashlib.sha256(encoded).hexdigest()
    counts = {
        "users": len(payload["users"]),
        "farm_sessions": len(payload["farm_sessions"]),
        "audit_events": len(payload["audit_events"]),
    }
    with connection.pool.connection() as database:
        with database.transaction():
            with database.cursor() as cursor:
                cursor.execute(
                    """SELECT source_sha256,users_count,farm_sessions_count,audit_events_count
                       FROM migration_receipts WHERE migration_id=%s FOR UPDATE""",
                    (MIGRATION_ID,),
                )
                existing = cursor.fetchone()
                if existing is not None:
                    if (
                        existing["source_sha256"] != source_sha256
                        or int(existing["users_count"]) != counts["users"]
                        or int(existing["farm_sessions_count"])
                        != counts["farm_sessions"]
                        or int(existing["audit_events_count"])
                        != counts["audit_events"]
                    ):
                        raise MigrationError("legacy_migration_receipt_conflict")
                    return {**counts, "source_sha256": source_sha256, "replayed": True}
                cursor.execute(
                    """SELECT
                         (SELECT count(*) FROM users) AS users_count,
                         (SELECT count(*) FROM farm_sessions) AS farm_sessions_count,
                         (SELECT count(*) FROM audit_events) AS audit_events_count"""
                )
                target_counts = cursor.fetchone()
                if target_counts is None or any(
                    int(target_counts[name]) != 0
                    for name in (
                        "users_count",
                        "farm_sessions_count",
                        "audit_events_count",
                    )
                ):
                    raise MigrationError("legacy_migration_target_not_empty")
                cursor.executemany(
                    """INSERT INTO users
                       (username,user_id,password_hash,role,created_at,updated_at)
                       VALUES (%s,%s,%s,%s,%s,%s)
                       """,
                    [
                        (
                            row["username"], row["user_id"], row["password_hash"], row["role"],
                            _timestamp_iso(row["created_at"]), _timestamp_iso(row["updated_at"]),
                        )
                        for row in payload["users"]
                    ],
                )
                cursor.executemany(
                    """INSERT INTO farm_sessions
                       (owner_username,session_id,client_session_id,payload_json,created_at,updated_at)
                       VALUES (%s,%s,%s,%s::jsonb,%s,%s)
                       """,
                    [
                        (
                            row["owner_username"], row["session_id"], row["client_session_id"],
                            json.dumps(row["payload"],ensure_ascii=False,separators=(",", ":")),
                            _timestamp_iso(row["created_at"]), _timestamp_iso(row["updated_at"]),
                        )
                        for row in payload["farm_sessions"]
                    ],
                )
                cursor.executemany(
                    """INSERT INTO audit_events
                       (event_id,occurred_at,kind,actor_user_id,target_user_id,request_id,outcome)
                       VALUES (%s,%s,%s,%s,%s,%s,%s)
                       """,
                    [
                        (
                            row["event_id"], _timestamp_iso(row["occurred_at"]), row["kind"],
                            row["actor_user_id"], row["target_user_id"], row["request_id"],
                            row["outcome"],
                        )
                        for row in payload["audit_events"]
                    ],
                )
                cursor.execute(
                    """INSERT INTO migration_receipts
                       (migration_id,source_sha256,users_count,farm_sessions_count,
                        audit_events_count,applied_at)
                       VALUES (%s,%s,%s,%s,%s,now())""",
                    (
                        MIGRATION_ID,
                        source_sha256,
                        counts["users"],
                        counts["farm_sessions"],
                        counts["audit_events"],
                    ),
                )
    return {**counts, "source_sha256": source_sha256, "replayed": False}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Private YDB to PostgreSQL state migration.")
    parser.add_argument("action", choices=("export-ydb", "import-postgres"))
    args = parser.parse_args(argv)
    try:
        if args.action == "export-ydb":
            bundle = export_ydb_bundle(
                YdbConnection(os.environ["YDB_ENDPOINT"], os.environ["YDB_DATABASE"])
            )
            sys.stdout.buffer.write(bundle)
            return 0
        encoded = sys.stdin.buffer.read(MAX_BUNDLE_BYTES + 1)
        counts = import_postgres_bundle(
            PostgresConnection(os.environ["POSTGRES_DSN"]), encoded
        )
        print(
            json.dumps(
                {
                    "status": "private_state_imported",
                    **counts,
                    "sessions_migrated": False,
                    "service_tokens_migrated": False,
                    "credentials_exported": False,
                },
                sort_keys=True,
                separators=(",", ":"),
            )
        )
        return 0
    except (KeyError, MigrationError, RuntimeError, OSError):
        print("private_state_migration_failed", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
