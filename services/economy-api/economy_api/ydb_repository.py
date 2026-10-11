from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, datetime
from typing import Any

from .models import (
    AuditEvent,
    CrawlerAction,
    CrawlerStatus,
    DesktopSyncToken,
    FarmSession,
    Role,
    Session,
    User,
)

try:  # Deploy-only dependency; the application core and tests remain stdlib-only.
    import ydb  # type: ignore
except ImportError:  # pragma: no cover - exercised only in the deployment bundle
    ydb = None

CRAWLER_LEASE_ACTIVE_SECONDS = 420


def _role(value: str) -> Role:
    if value not in {"registered", "approved", "admin"}:
        raise RuntimeError("invalid_role_in_database")
    return value  # type: ignore[return-value]


def _timestamp(value: object) -> datetime:
    """Normalize the two Timestamp representations returned by YDB SDKs."""
    if isinstance(value, datetime):
        return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)
    if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
        # The table API currently returns YQL Timestamp as Unix microseconds.
        return datetime.fromtimestamp(value / 1_000_000, tz=UTC)
    raise RuntimeError("invalid_timestamp_in_database")


def _user(row: Any) -> User:
    return User(
        username=row["username"],
        user_id=row["user_id"],
        password_hash=row["password_hash"],
        role=_role(row["role"]),
        created_at=_timestamp(row["created_at"]),
        updated_at=_timestamp(row["updated_at"]),
    )


def _farm(row: Any) -> FarmSession:
    return FarmSession(
        owner_username=row["owner_username"],
        session_id=row["session_id"],
        client_session_id=row["client_session_id"],
        payload=json.loads(row["payload_json"]),
        created_at=_timestamp(row["created_at"]),
        updated_at=_timestamp(row["updated_at"]),
    )


def _desktop_sync_token(row: Any) -> DesktopSyncToken:
    revoked = row["revoked_at"]
    return DesktopSyncToken(
        token_id=row["token_id"],
        token_digest=row["token_digest"],
        owner_username=row["owner_username"],
        scope=row["scope"],
        label=row["label"],
        created_at=_timestamp(row["created_at"]),
        expires_at=_timestamp(row["expires_at"]),
        revoked_at=_timestamp(revoked) if revoked is not None else None,
    )


def _crawler_action(row: Any) -> CrawlerAction:
    raw_status = row["status"]
    claimed_by = None
    status = raw_status
    if raw_status.startswith("claimed:"):
        status = "claimed"
        claimed_by = raw_status.removeprefix("claimed:")
    completed_at = (
        _timestamp(row["updated_at"])
        if raw_status in {"applied", "rejected", "failed"}
        else None
    )
    return CrawlerAction(
        action_id=row["action_id"],
        kind=row["kind"],
        expected_revision=row["expected_revision"],
        status=status,
        requested_by_user_id=row["requested_by_user_id"],
        created_at=_timestamp(row["created_at"]),
        updated_at=_timestamp(row["updated_at"]),
        claimed_by=claimed_by,
        completed_at=completed_at,
    )


def _crawler_status_document(row: CrawlerStatus) -> str:
    def stamp(value: datetime | None) -> str | None:
        return value.astimezone(UTC).isoformat().replace("+00:00", "Z") if value else None

    return json.dumps(
        {
            "runId": row.run_id,
            "status": row.status,
            "phase": row.phase,
            "revision": row.revision,
            "updatedAt": stamp(row.updated_at),
            "startedAt": stamp(row.started_at),
            "elapsedSeconds": row.elapsed_seconds,
            "totalItems": row.total_items,
            "processedItems": row.processed_items,
            "successfulRequests": row.successful_requests,
            "failedRequests": row.failed_requests,
            "retriedRequests": row.retried_requests,
            "queueDepth": row.queue_depth,
            "checkpoint": row.checkpoint,
            "errors": list(row.errors),
        },
        separators=(",", ":"),
    )


class YdbConnection:
    def __init__(self, endpoint: str, database: str, *, ready_timeout: float = 8) -> None:
        if ydb is None:
            raise RuntimeError("ydb_dependency_not_installed")
        config = ydb.DriverConfig(
            endpoint=endpoint,
            database=database,
            credentials=ydb.credentials_from_env_variables(),
        )
        self.driver = ydb.Driver(config)
        self.driver.wait(timeout=ready_timeout, fail_fast=True)
        self.pool = ydb.SessionPool(self.driver, size=5)

    def execute(self, query: str, params: dict[str, Any]) -> list[Any]:
        def operation(session: Any) -> list[Any]:
            # The YDB table API silently ignores ``parameters`` when a raw YQL
            # string is passed to TransactionContext.execute. Parameters are
            # supported only for a prepared DataQuery, so always prepare on the
            # session selected by the pool before executing.
            prepared = session.prepare(query)
            result = session.transaction().execute(prepared, params, commit_tx=True)
            return result[0].rows if result else []

        return self.pool.retry_operation_sync(operation)

    def execute_scheme(self, statement: str) -> None:
        self.pool.retry_operation_sync(lambda session: session.execute_scheme(statement))

    def add_column_if_missing(self, statement: str) -> None:
        if ydb is None:  # pragma: no cover - guarded by __init__ in production
            raise RuntimeError("ydb_dependency_not_installed")
        try:
            self.execute_scheme(statement)
        except ydb.AlreadyExists:
            # YDB ADD COLUMN has no IF NOT EXISTS form. This narrow helper is
            # used only for the one named migration statement, so repeat
            # schema runs remain idempotent without masking other DDL errors.
            return


class YdbRepository:
    def __init__(self, connection: YdbConnection) -> None:
        self.connection = connection

    def create_user(self, user: User) -> bool:
        query = """
        DECLARE $username AS Utf8;
        DECLARE $user_id AS Utf8;
        DECLARE $password_hash AS Utf8;
        DECLARE $role AS Utf8;
        DECLARE $created_at AS Timestamp;
        INSERT INTO users
          (username, user_id, password_hash, role, created_at, updated_at)
        VALUES
          ($username, $user_id, $password_hash, $role, $created_at, $created_at);
        """
        try:
            self.connection.execute(
                query,
                {
                    "$username": user.username,
                    "$user_id": user.user_id,
                    "$password_hash": user.password_hash,
                    "$role": user.role,
                    "$created_at": user.created_at,
                },
            )
            return True
        except Exception as exc:
            if exc.__class__.__name__ in {"PreconditionFailed", "AlreadyExists"}:
                return False
            raise

    def get_user(self, username: str) -> User | None:
        rows = self.connection.execute(
            """
            DECLARE $username AS Utf8;
            SELECT username, user_id, password_hash, role, created_at, updated_at
            FROM users WHERE username = $username;
            """,
            {"$username": username},
        )
        return _user(rows[0]) if rows else None

    def list_users(self, limit: int) -> list[User]:
        rows = self.connection.execute(
            """
            DECLARE $limit AS Uint64;
            SELECT username, user_id, password_hash, role, created_at, updated_at
            FROM users ORDER BY username LIMIT $limit;
            """,
            {"$limit": limit},
        )
        return [_user(row) for row in rows]

    def set_user_role(self, username: str, role: Role, now: datetime) -> User | None:
        self.connection.execute(
            """
            DECLARE $username AS Utf8;
            DECLARE $role AS Utf8;
            DECLARE $updated_at AS Timestamp;
            UPDATE users SET role = $role, updated_at = $updated_at
            WHERE username = $username;
            """,
            {"$username": username, "$role": role, "$updated_at": now},
        )
        return self.get_user(username)

    def set_user_password_hash(
        self, username: str, password_hash: str, now: datetime
    ) -> User | None:
        self.connection.execute(
            """
            DECLARE $username AS Utf8;
            DECLARE $password_hash AS Utf8;
            DECLARE $updated_at AS Timestamp;
            UPDATE users SET password_hash = $password_hash, updated_at = $updated_at
            WHERE username = $username;
            """,
            {
                "$username": username,
                "$password_hash": password_hash,
                "$updated_at": now,
            },
        )
        return self.get_user(username)

    def create_session(self, session: Session) -> None:
        self.connection.execute(
            """
            DECLARE $session_hash AS Utf8;
            DECLARE $username AS Utf8;
            DECLARE $csrf_hash AS Utf8;
            DECLARE $created_at AS Timestamp;
            DECLARE $expires_at AS Timestamp;
            UPSERT INTO sessions
              (session_hash, username, csrf_hash, created_at, expires_at)
            VALUES
              ($session_hash, $username, $csrf_hash, $created_at, $expires_at);
            """,
            {
                "$session_hash": session.session_hash,
                "$username": session.username,
                "$csrf_hash": session.csrf_hash,
                "$created_at": session.created_at,
                "$expires_at": session.expires_at,
            },
        )

    def get_session(self, session_hash: str) -> Session | None:
        rows = self.connection.execute(
            """
            DECLARE $session_hash AS Utf8;
            SELECT session_hash, username, csrf_hash, created_at, expires_at
            FROM sessions WHERE session_hash = $session_hash;
            """,
            {"$session_hash": session_hash},
        )
        if not rows:
            return None
        row = rows[0]
        return Session(
            session_hash=row["session_hash"],
            username=row["username"],
            csrf_hash=row["csrf_hash"],
            created_at=_timestamp(row["created_at"]),
            expires_at=_timestamp(row["expires_at"]),
        )

    def delete_session(self, session_hash: str) -> None:
        self.connection.execute(
            """
            DECLARE $session_hash AS Utf8;
            DELETE FROM sessions WHERE session_hash = $session_hash;
            """,
            {"$session_hash": session_hash},
        )

    def create_desktop_sync_token(self, token: DesktopSyncToken) -> None:
        self.connection.execute(
            """
            DECLARE $token_id AS Utf8;
            DECLARE $token_digest AS Utf8;
            DECLARE $owner AS Utf8;
            DECLARE $scope AS Utf8;
            DECLARE $label AS Optional<Utf8>;
            DECLARE $created_at AS Timestamp;
            DECLARE $expires_at AS Timestamp;
            INSERT INTO desktop_sync_tokens
              (token_id, token_digest, owner_username, scope, label,
               created_at, expires_at, revoked_at)
            VALUES
              ($token_id, $token_digest, $owner, $scope, $label,
               $created_at, $expires_at, NULL);
            """,
            {
                "$token_id": token.token_id,
                "$token_digest": token.token_digest,
                "$owner": token.owner_username,
                "$scope": token.scope,
                "$label": token.label,
                "$created_at": token.created_at,
                "$expires_at": token.expires_at,
            },
        )

    def get_desktop_sync_token(self, token_id: str) -> DesktopSyncToken | None:
        rows = self.connection.execute(
            """
            DECLARE $token_id AS Utf8;
            SELECT token_id, token_digest, owner_username, scope, label,
                   created_at, expires_at, revoked_at
            FROM desktop_sync_tokens WHERE token_id = $token_id;
            """,
            {"$token_id": token_id},
        )
        return _desktop_sync_token(rows[0]) if rows else None

    def list_desktop_sync_tokens(
        self, owner: str, limit: int
    ) -> list[DesktopSyncToken]:
        rows = self.connection.execute(
            """
            DECLARE $owner AS Utf8;
            DECLARE $limit AS Uint64;
            SELECT token_id, token_digest, owner_username, scope, label,
                   created_at, expires_at, revoked_at
            FROM desktop_sync_tokens
            WHERE owner_username = $owner
            ORDER BY created_at DESC LIMIT $limit;
            """,
            {"$owner": owner, "$limit": limit},
        )
        return [_desktop_sync_token(row) for row in rows]

    def revoke_desktop_sync_token(
        self, owner: str, token_id: str, now: datetime
    ) -> bool:
        token = self.get_desktop_sync_token(token_id)
        if token is None or token.owner_username != owner:
            return False
        self.connection.execute(
            """
            DECLARE $owner AS Utf8;
            DECLARE $token_id AS Utf8;
            DECLARE $revoked_at AS Timestamp;
            UPDATE desktop_sync_tokens SET revoked_at = $revoked_at
            WHERE owner_username = $owner AND token_id = $token_id;
            """,
            {"$owner": owner, "$token_id": token_id, "$revoked_at": now},
        )
        return True

    def upsert_farm_session(
        self, session: FarmSession, max_sessions: int
    ) -> FarmSession | None:
        # Mapping lookup and both writes share one serializable transaction.
        # Concurrent offline retries therefore either commit together or retry
        # against the winning mapping; a mapping cannot point at an uncommitted
        # farm row and a losing request cannot leave an orphan farm row.
        def operation(db_session: Any) -> FarmSession:
            transaction = db_session.transaction(ydb.SerializableReadWrite())
            mapping_query = db_session.prepare(
                """
                DECLARE $owner AS Utf8;
                DECLARE $client_id AS Utf8;
                SELECT session_id FROM farm_session_ids
                WHERE owner_username = $owner AND client_session_id = $client_id;
                """
            )
            mapping_result = transaction.execute(
                mapping_query,
                {"$owner": session.owner_username, "$client_id": session.client_session_id},
                commit_tx=False,
            )
            mapping_rows = mapping_result[0].rows if mapping_result else []
            resolved_id = mapping_rows[0]["session_id"] if mapping_rows else session.session_id

            existing_query = db_session.prepare(
                """
                DECLARE $owner AS Utf8;
                DECLARE $session_id AS Utf8;
                SELECT owner_username, session_id, client_session_id, payload_json,
                       created_at, updated_at
                FROM farm_sessions
                WHERE owner_username = $owner AND session_id = $session_id;
                """
            )
            existing_result = transaction.execute(
                existing_query,
                {"$owner": session.owner_username, "$session_id": resolved_id},
                commit_tx=False,
            )
            existing_rows = existing_result[0].rows if existing_result else []
            if not existing_rows:
                count_query = db_session.prepare(
                    """
                    DECLARE $owner AS Utf8;
                    SELECT COUNT(*) AS session_count
                    FROM farm_sessions WHERE owner_username = $owner;
                    """
                )
                count_result = transaction.execute(
                    count_query, {"$owner": session.owner_username}, commit_tx=False
                )
                count_rows = count_result[0].rows if count_result else []
                current_count = int(count_rows[0]["session_count"]) if count_rows else 0
                if current_count >= max_sessions:
                    transaction.execute(
                        db_session.prepare("SELECT 1 AS accepted;"), {}, commit_tx=True
                    )
                    return None
            stored = FarmSession(
                owner_username=session.owner_username,
                session_id=resolved_id,
                client_session_id=session.client_session_id,
                payload=session.payload,
                created_at=(
                    _timestamp(existing_rows[0]["created_at"])
                    if existing_rows
                    else session.created_at
                ),
                updated_at=session.updated_at,
            )
            write_query = db_session.prepare(
                """
                DECLARE $owner AS Utf8;
                DECLARE $session_id AS Utf8;
                DECLARE $client_id AS Utf8;
                DECLARE $payload AS JsonDocument;
                DECLARE $created_at AS Timestamp;
                DECLARE $updated_at AS Timestamp;
                UPSERT INTO farm_sessions
                  (owner_username, session_id, client_session_id, payload_json,
                   created_at, updated_at)
                VALUES
                  ($owner, $session_id, $client_id, $payload, $created_at, $updated_at);
                UPSERT INTO farm_session_ids
                  (owner_username, client_session_id, session_id, updated_at)
                VALUES ($owner, $client_id, $session_id, $updated_at);
                """
            )
            transaction.execute(
                write_query,
                {
                    "$owner": stored.owner_username,
                    "$session_id": stored.session_id,
                    "$client_id": stored.client_session_id,
                    "$payload": json.dumps(stored.payload, separators=(",", ":")),
                    "$created_at": stored.created_at,
                    "$updated_at": stored.updated_at,
                },
                commit_tx=True,
            )
            return stored

        return self.connection.pool.retry_operation_sync(operation)

    def count_farm_sessions(self, owner: str) -> int:
        rows = self.connection.execute(
            """
            DECLARE $owner AS Utf8;
            SELECT COUNT(*) AS session_count
            FROM farm_sessions WHERE owner_username = $owner;
            """,
            {"$owner": owner},
        )
        return int(rows[0]["session_count"]) if rows else 0

    def get_farm_session(self, owner: str, session_id: str) -> FarmSession | None:
        rows = self.connection.execute(
            """
            DECLARE $owner AS Utf8;
            DECLARE $session_id AS Utf8;
            SELECT owner_username, session_id, client_session_id, payload_json,
                   created_at, updated_at
            FROM farm_sessions
            WHERE owner_username = $owner AND session_id = $session_id;
            """,
            {"$owner": owner, "$session_id": session_id},
        )
        return _farm(rows[0]) if rows else None

    def list_farm_sessions(
        self, owner: str | None, limit: int, cursor: str | None
    ) -> tuple[list[FarmSession], str | None]:
        if owner is None:
            if cursor:
                try:
                    cursor_owner, cursor_session = cursor.split(":", 1)
                except ValueError:
                    return [], None
            else:
                cursor_owner = "\U0010ffff"
                cursor_session = "\U0010ffff"
            query = """
            DECLARE $cursor_owner AS Utf8;
            DECLARE $cursor_session AS Utf8;
            DECLARE $limit AS Uint64;
            SELECT owner_username, session_id, client_session_id, payload_json,
                   created_at, updated_at
            FROM farm_sessions
            WHERE owner_username < $cursor_owner OR
                  (owner_username = $cursor_owner AND session_id < $cursor_session)
            ORDER BY owner_username DESC, session_id DESC LIMIT $limit;
            """
            params: dict[str, Any] = {
                "$cursor_owner": cursor_owner,
                "$cursor_session": cursor_session,
                "$limit": limit + 1,
            }
        else:
            cursor_value = cursor or "\U0010ffff"
            query = """
            DECLARE $owner AS Utf8;
            DECLARE $cursor AS Utf8;
            DECLARE $limit AS Uint64;
            SELECT owner_username, session_id, client_session_id, payload_json,
                   created_at, updated_at
            FROM farm_sessions
            WHERE owner_username = $owner AND session_id < $cursor
            ORDER BY owner_username, session_id DESC LIMIT $limit;
            """
            params = {"$owner": owner, "$cursor": cursor_value, "$limit": limit + 1}
        rows = self.connection.execute(query, params)
        page = [_farm(row) for row in rows[:limit]]
        next_cursor = None
        if len(rows) > limit:
            last = page[-1]
            next_cursor = (
                f"{last.owner_username}:{last.session_id}"
                if owner is None else last.session_id
            )
        return page, next_cursor

    def delete_farm_session(self, owner: str, session_id: str) -> bool:
        current = self.get_farm_session(owner, session_id)
        if not current:
            return False
        self.connection.execute(
            """
            DECLARE $owner AS Utf8;
            DECLARE $session_id AS Utf8;
            DECLARE $client_id AS Utf8;
            DELETE FROM farm_sessions
            WHERE owner_username = $owner AND session_id = $session_id;
            DELETE FROM farm_session_ids
            WHERE owner_username = $owner AND client_session_id = $client_id;
            """,
            {
                "$owner": owner,
                "$session_id": session_id,
                "$client_id": current.client_session_id,
            },
        )
        return True

    def append_audit(self, event: AuditEvent) -> None:
        self.connection.execute(
            """
            DECLARE $event_id AS Utf8;
            DECLARE $occurred_at AS Timestamp;
            DECLARE $kind AS Utf8;
            DECLARE $actor AS Optional<Utf8>;
            DECLARE $target AS Optional<Utf8>;
            DECLARE $request_id AS Utf8;
            DECLARE $outcome AS Utf8;
            INSERT INTO audit_events
              (event_id, occurred_at, kind, actor_user_id, target_user_id, request_id, outcome)
            VALUES ($event_id, $occurred_at, $kind, $actor, $target, $request_id, $outcome);
            """,
            {
                "$event_id": event.event_id,
                "$occurred_at": event.occurred_at,
                "$kind": event.kind,
                "$actor": event.actor_user_id,
                "$target": event.target_user_id,
                "$request_id": event.request_id,
                "$outcome": event.outcome[:64],
            },
        )

    def get_crawler_status(self) -> CrawlerStatus | None:
        rows = self.connection.execute(
            """
            DECLARE $crawler_id AS Utf8;
            SELECT payload_json
            FROM crawler_status WHERE crawler_id = $crawler_id;
            """,
            {"$crawler_id": "primary"},
        )
        if not rows:
            return None
        value = json.loads(rows[0]["payload_json"])
        if not isinstance(value, dict):
            raise RuntimeError("invalid_crawler_status")

        def timestamp(name: str) -> datetime | None:
            raw = value.get(name)
            if raw is None:
                return None
            parsed = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
            return parsed.astimezone(UTC)

        def bounded_int(name: str) -> int:
            raw = value.get(name, 0)
            if not isinstance(raw, int) or isinstance(raw, bool) or not 0 <= raw <= 2**63 - 1:
                raise RuntimeError("invalid_crawler_status")
            return raw

        errors = value.get("errors", [])
        if not isinstance(errors, list) or len(errors) > 20:
            raise RuntimeError("invalid_crawler_status")
        safe_errors = []
        for item in errors:
            if not isinstance(item, dict):
                raise RuntimeError("invalid_crawler_status")
            required = {
                "errorId",
                "at",
                "code",
                "errorClass",
                "message",
                "retryable",
            }
            if (
                not required.issubset(item)
                or not set(item).issubset(required | {"itemId", "attempt"})
                or not all(
                    isinstance(item[name], str)
                    for name in ("errorId", "at", "code", "errorClass", "message")
                )
                or len(item["errorId"]) > 96
                or len(item["at"]) > 40
                or len(item["code"]) > 64
                or len(item["errorClass"]) > 96
                or len(item["message"]) > 240
                or not isinstance(item["retryable"], bool)
            ):
                raise RuntimeError("invalid_crawler_status")
            safe_error = {name: item[name] for name in required}
            if "itemId" in item:
                if not isinstance(item["itemId"], str) or len(item["itemId"]) > 64:
                    raise RuntimeError("invalid_crawler_status")
                safe_error["itemId"] = item["itemId"]
            if "attempt" in item:
                if (
                    not isinstance(item["attempt"], int)
                    or isinstance(item["attempt"], bool)
                    or not 0 <= item["attempt"] <= 1000
                ):
                    raise RuntimeError("invalid_crawler_status")
                safe_error["attempt"] = item["attempt"]
            safe_errors.append(safe_error)
        checkpoint = value.get("checkpoint")
        safe_checkpoint = None
        if checkpoint is not None:
            if (
                not isinstance(checkpoint, dict)
                or set(checkpoint) != {"cursor", "processedItems", "savedAt", "revision"}
                or not isinstance(checkpoint["cursor"], str)
                or len(checkpoint["cursor"]) > 128
                or not isinstance(checkpoint["savedAt"], str)
                or len(checkpoint["savedAt"]) > 40
            ):
                raise RuntimeError("invalid_crawler_status")
            for name in ("processedItems", "revision"):
                field = checkpoint[name]
                if (
                    not isinstance(field, int)
                    or isinstance(field, bool)
                    or not 0 <= field <= 2**63 - 1
                ):
                    raise RuntimeError("invalid_crawler_status")
            safe_checkpoint = dict(checkpoint)
        return CrawlerStatus(
            run_id=value.get("runId") if isinstance(value.get("runId"), str) else None,
            status=str(value.get("status", "unknown"))[:32],
            phase=str(value.get("phase", "unknown"))[:32],
            revision=bounded_int("revision"),
            updated_at=timestamp("updatedAt"),
            started_at=timestamp("startedAt"),
            elapsed_seconds=bounded_int("elapsedSeconds"),
            total_items=bounded_int("totalItems"),
            processed_items=bounded_int("processedItems"),
            successful_requests=bounded_int("successfulRequests"),
            failed_requests=bounded_int("failedRequests"),
            retried_requests=bounded_int("retriedRequests"),
            queue_depth=bounded_int("queueDepth"),
            checkpoint=safe_checkpoint,
            errors=tuple(safe_errors),
        )

    def _get_crawler_action(self, action_id: str) -> CrawlerAction | None:
        rows = self.connection.execute(
            """
            DECLARE $action_id AS Utf8;
            SELECT action_id, kind, expected_revision, status, requested_by_user_id,
                   created_at, updated_at
            FROM crawler_actions WHERE action_id = $action_id;
            """,
            {"$action_id": action_id},
        )
        return _crawler_action(rows[0]) if rows else None

    def connect_crawler(
        self,
        crawler_id: str,
        run_id: str,
        lease_digest: str,
        initial_status: CrawlerStatus,
        now: datetime,
    ) -> CrawlerStatus | None:
        def operation(db_session: Any) -> CrawlerStatus | None:
            transaction = db_session.transaction(ydb.SerializableReadWrite())
            lease_query = db_session.prepare(
                """
                DECLARE $crawler_id AS Utf8;
                SELECT updated_at FROM crawler_leases
                WHERE crawler_id = $crawler_id;
                """
            )
            lease_result = transaction.execute(
                lease_query, {"$crawler_id": crawler_id}, commit_tx=False
            )
            lease_rows = lease_result[0].rows if lease_result else []
            if lease_rows:
                existing_updated_at = _timestamp(lease_rows[0]["updated_at"])
                lease_age = (now - existing_updated_at).total_seconds()
                if 0 <= lease_age < CRAWLER_LEASE_ACTIVE_SECONDS:
                    transaction.commit()
                    return None
            read_query = db_session.prepare(
                """
                DECLARE $crawler_id AS Utf8;
                SELECT payload_json FROM crawler_status WHERE crawler_id = $crawler_id;
                """
            )
            result = transaction.execute(
                read_query, {"$crawler_id": crawler_id}, commit_tx=False
            )
            rows = result[0].rows if result else []
            current_revision = 0
            if rows:
                current = json.loads(rows[0]["payload_json"])
                if isinstance(current, dict):
                    current_revision = int(current.get("revision", 0))
            connected = CrawlerStatus(
                run_id=run_id,
                status=initial_status.status,
                phase=initial_status.phase,
                revision=current_revision + 1,
                updated_at=now,
                started_at=initial_status.started_at,
                elapsed_seconds=initial_status.elapsed_seconds,
                total_items=initial_status.total_items,
                processed_items=initial_status.processed_items,
                successful_requests=initial_status.successful_requests,
                failed_requests=initial_status.failed_requests,
                retried_requests=initial_status.retried_requests,
                queue_depth=initial_status.queue_depth,
                checkpoint=initial_status.checkpoint,
                errors=initial_status.errors,
            )
            write_query = db_session.prepare(
                """
                DECLARE $crawler_id AS Utf8;
                DECLARE $run_id AS Utf8;
                DECLARE $lease_digest AS Utf8;
                DECLARE $payload AS JsonDocument;
                DECLARE $revision AS Int64;
                DECLARE $claimed_status AS Utf8;
                DECLARE $now AS Timestamp;
                UPSERT INTO crawler_status (crawler_id, payload_json, updated_at)
                VALUES ($crawler_id, $payload, $now);
                UPSERT INTO crawler_leases
                  (crawler_id, run_id, lease_digest, connected_at, updated_at)
                VALUES ($crawler_id, $run_id, $lease_digest, $now, $now);
                UPDATE crawler_actions
                SET status = "queued", expected_revision = $revision,
                    updated_at = $now
                WHERE status = $claimed_status;
                """
            )
            transaction.execute(
                write_query,
                {
                    "$crawler_id": crawler_id,
                    "$run_id": run_id,
                    "$lease_digest": lease_digest,
                    "$payload": _crawler_status_document(connected),
                    "$revision": connected.revision,
                    "$claimed_status": f"claimed:{crawler_id}",
                    "$now": now,
                },
                commit_tx=True,
            )
            return connected

        return self.connection.pool.retry_operation_sync(operation)

    def delete_crawler_state_if_owned(self, crawler_id: str, run_id: str) -> bool:
        """Delete smoke crawler state only while the same run owns both rows."""

        def operation(db_session: Any) -> bool:
            transaction = db_session.transaction(ydb.SerializableReadWrite())
            lease_query = db_session.prepare(
                """
                DECLARE $crawler_id AS Utf8;
                SELECT run_id FROM crawler_leases WHERE crawler_id = $crawler_id;
                """
            )
            lease_result = transaction.execute(
                lease_query, {"$crawler_id": crawler_id}, commit_tx=False
            )
            lease_rows = lease_result[0].rows if lease_result else []
            status_query = db_session.prepare(
                """
                DECLARE $crawler_id AS Utf8;
                SELECT payload_json FROM crawler_status WHERE crawler_id = $crawler_id;
                """
            )
            status_result = transaction.execute(
                status_query, {"$crawler_id": crawler_id}, commit_tx=False
            )
            status_rows = status_result[0].rows if status_result else []
            try:
                status_run_id = json.loads(status_rows[0]["payload_json"])["runId"]
            except (IndexError, KeyError, TypeError, json.JSONDecodeError):
                status_run_id = None
            if (
                not lease_rows
                or lease_rows[0]["run_id"] != run_id
                or status_run_id != run_id
            ):
                transaction.commit()
                return False
            delete_query = db_session.prepare(
                """
                DECLARE $crawler_id AS Utf8;
                DECLARE $run_id AS Utf8;
                DELETE FROM crawler_leases
                WHERE crawler_id = $crawler_id AND run_id = $run_id;
                DELETE FROM crawler_status WHERE crawler_id = $crawler_id;
                """
            )
            transaction.execute(
                delete_query,
                {"$crawler_id": crawler_id, "$run_id": run_id},
                commit_tx=True,
            )
            return True

        return self.connection.pool.retry_operation_sync(operation)

    def publish_crawler_status_cas(
        self,
        crawler_id: str,
        run_id: str,
        lease_digest: str,
        expected_revision: int,
        status: CrawlerStatus,
        now: datetime,
    ) -> CrawlerStatus | None:
        def operation(db_session: Any) -> CrawlerStatus | None:
            transaction = db_session.transaction(ydb.SerializableReadWrite())
            lease_query = db_session.prepare(
                """
                DECLARE $crawler_id AS Utf8;
                SELECT run_id, lease_digest FROM crawler_leases
                WHERE crawler_id = $crawler_id;
                """
            )
            lease_result = transaction.execute(
                lease_query, {"$crawler_id": crawler_id}, commit_tx=False
            )
            lease_rows = lease_result[0].rows if lease_result else []
            if (
                not lease_rows
                or lease_rows[0]["run_id"] != run_id
                or lease_rows[0]["lease_digest"] != lease_digest
            ):
                transaction.commit()
                return None
            read_query = db_session.prepare(
                """
                DECLARE $crawler_id AS Utf8;
                SELECT payload_json FROM crawler_status WHERE crawler_id = $crawler_id;
                """
            )
            result = transaction.execute(
                read_query, {"$crawler_id": crawler_id}, commit_tx=False
            )
            rows = result[0].rows if result else []
            current_revision = 0
            if rows:
                value = json.loads(rows[0]["payload_json"])
                current_revision = value.get("revision", -1) if isinstance(value, dict) else -1
            if (
                current_revision != expected_revision
                or status.revision != expected_revision + 1
                or status.run_id != run_id
            ):
                transaction.commit()
                return None
            updated_status = replace(status, updated_at=now)
            write_query = db_session.prepare(
                """
                DECLARE $crawler_id AS Utf8;
                DECLARE $payload AS JsonDocument;
                DECLARE $updated_at AS Timestamp;
                DECLARE $revision AS Int64;
                UPSERT INTO crawler_status (crawler_id, payload_json, updated_at)
                VALUES ($crawler_id, $payload, $updated_at);
                UPDATE crawler_leases SET updated_at = $updated_at
                WHERE crawler_id = $crawler_id;
                UPDATE crawler_actions
                SET status = "rejected", updated_at = $updated_at
                WHERE status = "queued" AND expected_revision < $revision;
                """
            )
            transaction.execute(
                write_query,
                {
                    "$crawler_id": crawler_id,
                    "$payload": _crawler_status_document(updated_status),
                    "$updated_at": now,
                    "$revision": updated_status.revision,
                },
                commit_tx=True,
            )
            return updated_status

        return self.connection.pool.retry_operation_sync(operation)

    def enqueue_crawler_action(self, action: CrawlerAction) -> CrawlerAction | None:
        def operation(db_session: Any) -> CrawlerAction | None:
            transaction = db_session.transaction(ydb.SerializableReadWrite())
            action_query = db_session.prepare(
                """
                DECLARE $action_id AS Utf8;
                SELECT action_id, kind, expected_revision, status, requested_by_user_id,
                       created_at, updated_at
                FROM crawler_actions WHERE action_id = $action_id;
                """
            )
            result = transaction.execute(
                action_query, {"$action_id": action.action_id}, commit_tx=False
            )
            rows = result[0].rows if result else []
            if rows:
                transaction.commit()
                return _crawler_action(rows[0])
            status_query = db_session.prepare(
                """
                DECLARE $crawler_id AS Utf8;
                SELECT payload_json FROM crawler_status WHERE crawler_id = $crawler_id;
                """
            )
            result = transaction.execute(
                status_query, {"$crawler_id": "primary"}, commit_tx=False
            )
            status_rows = result[0].rows if result else []
            if not status_rows:
                transaction.commit()
                return None
            status_value = json.loads(status_rows[0]["payload_json"])
            if (
                not isinstance(status_value, dict)
                or status_value.get("revision") != action.expected_revision
            ):
                transaction.commit()
                return None
            insert_query = db_session.prepare(
                """
                DECLARE $action_id AS Utf8;
                DECLARE $kind AS Utf8;
                DECLARE $expected_revision AS Int64;
                DECLARE $status AS Utf8;
                DECLARE $requested_by AS Utf8;
                DECLARE $created_at AS Timestamp;
                INSERT INTO crawler_actions
                  (action_id, kind, expected_revision, status, requested_by_user_id,
                   created_at, updated_at)
                VALUES
                  ($action_id, $kind, $expected_revision, $status, $requested_by,
                   $created_at, $created_at);
                """
            )
            transaction.execute(
                insert_query,
                {
                    "$action_id": action.action_id,
                    "$kind": action.kind,
                    "$expected_revision": action.expected_revision,
                    "$status": action.status,
                    "$requested_by": action.requested_by_user_id,
                    "$created_at": action.created_at,
                },
                commit_tx=True,
            )
            return action

        return self.connection.pool.retry_operation_sync(operation)

    def claim_crawler_action(
        self,
        crawler_id: str,
        run_id: str,
        lease_digest: str,
        current_revision: int,
        now: datetime,
    ) -> CrawlerAction | None:
        def operation(db_session: Any) -> CrawlerAction | None:
            transaction = db_session.transaction(ydb.SerializableReadWrite())
            lease_query = db_session.prepare(
                """
                DECLARE $crawler_id AS Utf8;
                SELECT run_id, lease_digest FROM crawler_leases
                WHERE crawler_id = $crawler_id;
                """
            )
            lease_result = transaction.execute(
                lease_query, {"$crawler_id": crawler_id}, commit_tx=False
            )
            lease_rows = lease_result[0].rows if lease_result else []
            if (
                not lease_rows
                or lease_rows[0]["run_id"] != run_id
                or lease_rows[0]["lease_digest"] != lease_digest
            ):
                transaction.commit()
                return None
            status_query = db_session.prepare(
                """
                DECLARE $crawler_id AS Utf8;
                SELECT payload_json FROM crawler_status WHERE crawler_id = $crawler_id;
                """
            )
            status_result = transaction.execute(
                status_query, {"$crawler_id": crawler_id}, commit_tx=False
            )
            status_rows = status_result[0].rows if status_result else []
            if not status_rows:
                transaction.commit()
                return None
            status_value = json.loads(status_rows[0]["payload_json"])
            if (
                not isinstance(status_value, dict)
                or status_value.get("runId") != run_id
                or status_value.get("revision") != current_revision
            ):
                transaction.commit()
                return None
            action_query = db_session.prepare(
                """
                DECLARE $revision AS Int64;
                SELECT action_id, kind, expected_revision, status, requested_by_user_id,
                       created_at, updated_at
                FROM crawler_actions
                WHERE status = "queued" AND expected_revision = $revision
                ORDER BY created_at, action_id LIMIT 1;
                """
            )
            action_result = transaction.execute(
                action_query, {"$revision": current_revision}, commit_tx=False
            )
            rows = action_result[0].rows if action_result else []
            if not rows:
                transaction.commit()
                return None
            action = _crawler_action(rows[0])
            update_query = db_session.prepare(
                """
                DECLARE $action_id AS Utf8;
                DECLARE $claimed_status AS Utf8;
                DECLARE $updated_at AS Timestamp;
                UPDATE crawler_actions
                SET status = $claimed_status, updated_at = $updated_at
                WHERE action_id = $action_id;
                """
            )
            transaction.execute(
                update_query,
                {
                    "$action_id": action.action_id,
                    "$claimed_status": f"claimed:{crawler_id}",
                    "$updated_at": now,
                },
                commit_tx=True,
            )
            return CrawlerAction(
                action_id=action.action_id,
                kind=action.kind,
                expected_revision=action.expected_revision,
                status="claimed",
                requested_by_user_id=action.requested_by_user_id,
                created_at=action.created_at,
                updated_at=now,
                claimed_by=crawler_id,
            )

        return self.connection.pool.retry_operation_sync(operation)

    def complete_crawler_action(
        self,
        crawler_id: str,
        run_id: str,
        lease_digest: str,
        action_id: str,
        current_revision: int,
        outcome: str,
        now: datetime,
    ) -> CrawlerAction | None:
        def operation(db_session: Any) -> CrawlerAction | None:
            transaction = db_session.transaction(ydb.SerializableReadWrite())
            lease_query = db_session.prepare(
                """
                DECLARE $crawler_id AS Utf8;
                SELECT run_id, lease_digest FROM crawler_leases
                WHERE crawler_id = $crawler_id;
                """
            )
            lease_result = transaction.execute(
                lease_query, {"$crawler_id": crawler_id}, commit_tx=False
            )
            lease_rows = lease_result[0].rows if lease_result else []
            if (
                not lease_rows
                or lease_rows[0]["run_id"] != run_id
                or lease_rows[0]["lease_digest"] != lease_digest
            ):
                transaction.commit()
                return None
            status_query = db_session.prepare(
                """
                DECLARE $crawler_id AS Utf8;
                SELECT payload_json FROM crawler_status WHERE crawler_id = $crawler_id;
                """
            )
            status_result = transaction.execute(
                status_query, {"$crawler_id": crawler_id}, commit_tx=False
            )
            status_rows = status_result[0].rows if status_result else []
            if not status_rows:
                transaction.commit()
                return None
            status_value = json.loads(status_rows[0]["payload_json"])
            if (
                not isinstance(status_value, dict)
                or status_value.get("runId") != run_id
                or status_value.get("revision") != current_revision
            ):
                transaction.commit()
                return None
            action_query = db_session.prepare(
                """
                DECLARE $action_id AS Utf8;
                SELECT action_id, kind, expected_revision, status, requested_by_user_id,
                       created_at, updated_at
                FROM crawler_actions WHERE action_id = $action_id;
                """
            )
            action_result = transaction.execute(
                action_query, {"$action_id": action_id}, commit_tx=False
            )
            rows = action_result[0].rows if action_result else []
            if not rows:
                transaction.commit()
                return None
            action = _crawler_action(rows[0])
            if action.status != "claimed" or action.claimed_by != crawler_id:
                transaction.commit()
                return None
            update_query = db_session.prepare(
                """
                DECLARE $action_id AS Utf8;
                DECLARE $outcome AS Utf8;
                DECLARE $completed_at AS Timestamp;
                UPDATE crawler_actions
                SET status = $outcome, updated_at = $completed_at
                WHERE action_id = $action_id;
                """
            )
            transaction.execute(
                update_query,
                {"$action_id": action_id, "$outcome": outcome, "$completed_at": now},
                commit_tx=True,
            )
            return CrawlerAction(
                action_id=action.action_id,
                kind=action.kind,
                expected_revision=action.expected_revision,
                status=outcome,
                requested_by_user_id=action.requested_by_user_id,
                created_at=action.created_at,
                updated_at=now,
                claimed_by=crawler_id,
                completed_at=now,
            )

        return self.connection.pool.retry_operation_sync(operation)

    def list_crawler_actions(self, limit: int) -> list[CrawlerAction]:
        rows = self.connection.execute(
            """
            DECLARE $limit AS Uint64;
            SELECT action_id, kind, expected_revision, status, requested_by_user_id,
                   created_at, updated_at
            FROM crawler_actions ORDER BY created_at DESC LIMIT $limit;
            """,
            {"$limit": limit},
        )
        return [_crawler_action(row) for row in rows]

    def consume_rate_limit(
        self, key: str, limit: int, window_seconds: int, now: datetime
    ) -> bool:
        window = int(now.timestamp()) // window_seconds

        def operation(db_session: Any) -> bool:
            transaction = db_session.transaction(ydb.SerializableReadWrite())
            read_query = db_session.prepare(
                """
                DECLARE $key AS Utf8;
                DECLARE $window AS Int64;
                SELECT request_count FROM rate_limits
                WHERE limit_key = $key AND window_id = $window;
                """
            )
            rows = transaction.execute(
                read_query,
                {"$key": key, "$window": window},
                commit_tx=False,
            )[0].rows
            count = rows[0]["request_count"] if rows else 0
            if count >= limit:
                transaction.commit()
                return False
            write_query = db_session.prepare(
                """
                DECLARE $key AS Utf8;
                DECLARE $window AS Int64;
                DECLARE $count AS Uint64;
                DECLARE $expires_at AS Timestamp;
                UPSERT INTO rate_limits (limit_key, window_id, request_count, expires_at)
                VALUES ($key, $window, $count, $expires_at);
                """
            )
            transaction.execute(
                write_query,
                {
                    "$key": key,
                    "$window": window,
                    "$count": count + 1,
                    "$expires_at": datetime.fromtimestamp(
                        (window + 2) * window_seconds, tz=UTC
                    ),
                },
                commit_tx=True,
            )
            return True

        return self.connection.pool.retry_operation_sync(operation)
