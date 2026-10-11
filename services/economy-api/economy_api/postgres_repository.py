from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, datetime
from typing import Any, Iterator

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

try:  # Deployment-only dependencies.
    from psycopg.rows import dict_row  # type: ignore
    from psycopg_pool import ConnectionPool  # type: ignore
except ImportError:  # pragma: no cover - deployment image supplies these.
    dict_row = None
    ConnectionPool = None


CRAWLER_LEASE_ACTIVE_SECONDS = 420


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _role(value: str) -> Role:
    if value not in {"registered", "approved", "admin"}:
        raise RuntimeError("invalid_role_in_database")
    return value  # type: ignore[return-value]


def _user(row: dict[str, Any]) -> User:
    return User(
        username=str(row["username"]),
        user_id=str(row["user_id"]),
        password_hash=str(row["password_hash"]),
        role=_role(str(row["role"])),
        created_at=_utc(row["created_at"]),
        updated_at=_utc(row["updated_at"]),
    )


def _session(row: dict[str, Any]) -> Session:
    return Session(
        session_hash=str(row["session_hash"]),
        username=str(row["username"]),
        csrf_hash=str(row["csrf_hash"]),
        created_at=_utc(row["created_at"]),
        expires_at=_utc(row["expires_at"]),
    )


def _token(row: dict[str, Any]) -> DesktopSyncToken:
    revoked_at = row.get("revoked_at")
    return DesktopSyncToken(
        token_id=str(row["token_id"]),
        token_digest=str(row["token_digest"]),
        owner_username=str(row["owner_username"]),
        scope=str(row["scope"]),
        label=None if row.get("label") is None else str(row["label"]),
        created_at=_utc(row["created_at"]),
        expires_at=_utc(row["expires_at"]),
        revoked_at=None if revoked_at is None else _utc(revoked_at),
    )


def _farm(row: dict[str, Any]) -> FarmSession:
    payload = row["payload_json"]
    if isinstance(payload, str):
        payload = json.loads(payload)
    if not isinstance(payload, dict):
        raise RuntimeError("invalid_farm_payload_in_database")
    return FarmSession(
        owner_username=str(row["owner_username"]),
        session_id=str(row["session_id"]),
        client_session_id=str(row["client_session_id"]),
        payload=payload,
        created_at=_utc(row["created_at"]),
        updated_at=_utc(row["updated_at"]),
    )


def _action(row: dict[str, Any]) -> CrawlerAction:
    raw_status = str(row["status"])
    status = raw_status
    claimed_by = None
    if raw_status.startswith("claimed:"):
        status = "claimed"
        claimed_by = raw_status.removeprefix("claimed:")
    completed_at = (
        _utc(row["updated_at"])
        if raw_status in {"applied", "rejected", "failed"}
        else None
    )
    return CrawlerAction(
        action_id=str(row["action_id"]),
        kind=str(row["kind"]),
        expected_revision=int(row["expected_revision"]),
        status=status,
        requested_by_user_id=str(row["requested_by_user_id"]),
        created_at=_utc(row["created_at"]),
        updated_at=_utc(row["updated_at"]),
        claimed_by=claimed_by,
        completed_at=completed_at,
    )


def _status_document(row: CrawlerStatus) -> dict[str, Any]:
    def stamp(value: datetime | None) -> str | None:
        return _utc(value).isoformat().replace("+00:00", "Z") if value else None

    return {
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
    }


def _status(value: object) -> CrawlerStatus:
    if isinstance(value, str):
        value = json.loads(value)
    if not isinstance(value, dict):
        raise RuntimeError("invalid_crawler_status")

    def timestamp(name: str) -> datetime | None:
        raw = value.get(name)
        if raw is None:
            return None
        return datetime.fromisoformat(str(raw).replace("Z", "+00:00")).astimezone(UTC)

    def number(name: str) -> int:
        raw = value.get(name, 0)
        if not isinstance(raw, int) or isinstance(raw, bool) or not 0 <= raw < 2**63:
            raise RuntimeError("invalid_crawler_status")
        return raw

    errors = value.get("errors", [])
    checkpoint = value.get("checkpoint")
    if not isinstance(errors, list) or len(errors) > 20:
        raise RuntimeError("invalid_crawler_status")
    if checkpoint is not None and not isinstance(checkpoint, dict):
        raise RuntimeError("invalid_crawler_status")
    return CrawlerStatus(
        run_id=value.get("runId") if isinstance(value.get("runId"), str) else None,
        status=str(value.get("status", "unknown"))[:32],
        phase=str(value.get("phase", "unknown"))[:32],
        revision=number("revision"),
        updated_at=timestamp("updatedAt"),
        started_at=timestamp("startedAt"),
        elapsed_seconds=number("elapsedSeconds"),
        total_items=number("totalItems"),
        processed_items=number("processedItems"),
        successful_requests=number("successfulRequests"),
        failed_requests=number("failedRequests"),
        retried_requests=number("retriedRequests"),
        queue_depth=number("queueDepth"),
        checkpoint=None if checkpoint is None else dict(checkpoint),
        errors=tuple(dict(item) for item in errors if isinstance(item, dict)),
    )


class PostgresConnection:
    def __init__(self, dsn: str, *, min_size: int = 0, max_size: int = 5) -> None:
        if ConnectionPool is None or dict_row is None:
            raise RuntimeError("postgres_dependency_not_installed")
        if not dsn:
            raise ValueError("postgres_dsn_required")
        self.pool = ConnectionPool(
            conninfo=dsn,
            min_size=min_size,
            max_size=max_size,
            kwargs={"row_factory": dict_row, "autocommit": False},
            open=True,
        )
        self.pool.wait(timeout=8)


class PostgresRepository:
    """PostgreSQL implementation with server-authoritative transactional CAS."""

    def __init__(self, connection: PostgresConnection) -> None:
        self.connection = connection

    def _rows(self, query: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
        with self.connection.pool.connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(query, params)
                return list(cursor.fetchall())

    def create_user(self, user: User) -> bool:
        rows = self._rows(
            """INSERT INTO users
               (username,user_id,password_hash,role,created_at,updated_at)
               VALUES (%s,%s,%s,%s,%s,%s)
               ON CONFLICT (username) DO NOTHING RETURNING username""",
            (
                user.username,
                user.user_id,
                user.password_hash,
                user.role,
                user.created_at,
                user.updated_at,
            ),
        )
        return bool(rows)

    def get_user(self, username: str) -> User | None:
        rows = self._rows("SELECT * FROM users WHERE username=%s", (username,))
        return _user(rows[0]) if rows else None

    def list_users(self, limit: int) -> list[User]:
        return [
            _user(row)
            for row in self._rows(
                "SELECT * FROM users ORDER BY created_at,username LIMIT %s", (limit,)
            )
        ]

    def set_user_role(self, username: str, role: Role, now: datetime) -> User | None:
        rows = self._rows(
            "UPDATE users SET role=%s,updated_at=%s WHERE username=%s RETURNING *",
            (role, now, username),
        )
        return _user(rows[0]) if rows else None

    def set_user_password_hash(
        self, username: str, password_hash: str, now: datetime
    ) -> User | None:
        rows = self._rows(
            "UPDATE users SET password_hash=%s,updated_at=%s WHERE username=%s RETURNING *",
            (password_hash, now, username),
        )
        return _user(rows[0]) if rows else None

    def create_session(self, session: Session) -> None:
        self._rows(
            """INSERT INTO sessions
               (session_hash,username,csrf_hash,created_at,expires_at)
               VALUES (%s,%s,%s,%s,%s)
               ON CONFLICT (session_hash) DO UPDATE SET
                 username=EXCLUDED.username,csrf_hash=EXCLUDED.csrf_hash,
                 created_at=EXCLUDED.created_at,expires_at=EXCLUDED.expires_at
               RETURNING session_hash""",
            (
                session.session_hash,
                session.username,
                session.csrf_hash,
                session.created_at,
                session.expires_at,
            ),
        )

    def get_session(self, session_hash: str) -> Session | None:
        rows = self._rows(
            "SELECT * FROM sessions WHERE session_hash=%s AND expires_at>now()",
            (session_hash,),
        )
        return _session(rows[0]) if rows else None

    def delete_session(self, session_hash: str) -> None:
        self._rows(
            "DELETE FROM sessions WHERE session_hash=%s RETURNING session_hash",
            (session_hash,),
        )

    def create_desktop_sync_token(self, token: DesktopSyncToken) -> None:
        rows = self._rows(
            """INSERT INTO desktop_sync_tokens
               (token_id,token_digest,owner_username,scope,label,created_at,expires_at,revoked_at)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
               ON CONFLICT (token_id) DO NOTHING RETURNING token_id""",
            (
                token.token_id,
                token.token_digest,
                token.owner_username,
                token.scope,
                token.label,
                token.created_at,
                token.expires_at,
                token.revoked_at,
            ),
        )
        if not rows:
            raise RuntimeError("desktop_sync_token_id_collision")

    def get_desktop_sync_token(self, token_id: str) -> DesktopSyncToken | None:
        rows = self._rows(
            "SELECT * FROM desktop_sync_tokens WHERE token_id=%s AND expires_at>now()",
            (token_id,),
        )
        return _token(rows[0]) if rows else None

    def list_desktop_sync_tokens(
        self, owner: str, limit: int
    ) -> list[DesktopSyncToken]:
        return [
            _token(row)
            for row in self._rows(
                """SELECT * FROM desktop_sync_tokens
                   WHERE owner_username=%s AND expires_at>now()
                   ORDER BY created_at DESC,token_id DESC LIMIT %s""",
                (owner, limit),
            )
        ]

    def revoke_desktop_sync_token(
        self, owner: str, token_id: str, now: datetime
    ) -> bool:
        rows = self._rows(
            """UPDATE desktop_sync_tokens
               SET revoked_at=COALESCE(revoked_at,%s)
               WHERE token_id=%s AND owner_username=%s RETURNING token_id""",
            (now, token_id, owner),
        )
        return bool(rows)

    def upsert_farm_session(
        self, session: FarmSession, max_sessions: int
    ) -> FarmSession | None:
        with self.connection.pool.connection() as connection:
            with connection.transaction():
                with connection.cursor() as cursor:
                    cursor.execute(
                        "SELECT pg_advisory_xact_lock(hashtextextended(%s,0))",
                        (session.owner_username,),
                    )
                    cursor.execute(
                        """SELECT * FROM farm_sessions
                           WHERE owner_username=%s AND client_session_id=%s
                           FOR UPDATE""",
                        (session.owner_username, session.client_session_id),
                    )
                    existing = cursor.fetchone()
                    if existing is not None:
                        session = replace(
                            session,
                            session_id=str(existing["session_id"]),
                            created_at=_utc(existing["created_at"]),
                        )
                    else:
                        cursor.execute(
                            "SELECT count(*) AS count FROM farm_sessions WHERE owner_username=%s",
                            (session.owner_username,),
                        )
                        if int(cursor.fetchone()["count"]) >= max_sessions:
                            return None
                    cursor.execute(
                        """INSERT INTO farm_sessions
                           (owner_username,session_id,client_session_id,payload_json,created_at,updated_at)
                           VALUES (%s,%s,%s,%s::jsonb,%s,%s)
                           ON CONFLICT (owner_username,session_id) DO UPDATE SET
                             client_session_id=EXCLUDED.client_session_id,
                             payload_json=EXCLUDED.payload_json,
                             updated_at=EXCLUDED.updated_at
                           RETURNING *""",
                        (
                            session.owner_username,
                            session.session_id,
                            session.client_session_id,
                            json.dumps(session.payload, separators=(",", ":")),
                            session.created_at,
                            session.updated_at,
                        ),
                    )
                    return _farm(cursor.fetchone())

    def count_farm_sessions(self, owner: str) -> int:
        rows = self._rows(
            "SELECT count(*) AS count FROM farm_sessions WHERE owner_username=%s",
            (owner,),
        )
        return int(rows[0]["count"])

    def get_farm_session(self, owner: str, session_id: str) -> FarmSession | None:
        rows = self._rows(
            "SELECT * FROM farm_sessions WHERE owner_username=%s AND session_id=%s",
            (owner, session_id),
        )
        return _farm(rows[0]) if rows else None

    def list_farm_sessions(
        self, owner: str | None, limit: int, cursor: str | None
    ) -> tuple[list[FarmSession], str | None]:
        conditions: list[str] = []
        params: list[Any] = []
        if owner is not None:
            conditions.append("owner_username=%s")
            params.append(owner)
            if cursor:
                conditions.append("session_id<%s")
                params.append(cursor)
        elif cursor:
            try:
                cursor_owner, cursor_session = cursor.split(":", 1)
            except ValueError:
                return [], None
            conditions.append("(owner_username,session_id)<(%s,%s)")
            params.extend((cursor_owner, cursor_session))
        where = " WHERE " + " AND ".join(conditions) if conditions else ""
        params.append(limit + 1)
        rows = self._rows(
            "SELECT * FROM farm_sessions"
            + where
            + " ORDER BY owner_username DESC,session_id DESC LIMIT %s",
            tuple(params),
        )
        page = [_farm(row) for row in rows[:limit]]
        next_cursor = None
        if len(rows) > limit and page:
            last = page[-1]
            next_cursor = (
                last.session_id
                if owner is not None
                else f"{last.owner_username}:{last.session_id}"
            )
        return page, next_cursor

    def delete_farm_session(self, owner: str, session_id: str) -> bool:
        return bool(
            self._rows(
                """DELETE FROM farm_sessions
                   WHERE owner_username=%s AND session_id=%s RETURNING session_id""",
                (owner, session_id),
            )
        )

    def append_audit(self, event: AuditEvent) -> None:
        self._rows(
            """INSERT INTO audit_events
               (event_id,occurred_at,kind,actor_user_id,target_user_id,request_id,outcome)
               VALUES (%s,%s,%s,%s,%s,%s,%s) RETURNING event_id""",
            (
                event.event_id,
                event.occurred_at,
                event.kind,
                event.actor_user_id,
                event.target_user_id,
                event.request_id,
                event.outcome[:64],
            ),
        )

    def get_crawler_status(self) -> CrawlerStatus | None:
        rows = self._rows(
            "SELECT payload_json FROM crawler_status WHERE crawler_id='primary'"
        )
        return _status(rows[0]["payload_json"]) if rows else None

    def connect_crawler(
        self,
        crawler_id: str,
        run_id: str,
        lease_digest: str,
        initial_status: CrawlerStatus,
        now: datetime,
    ) -> CrawlerStatus | None:
        if crawler_id != "primary":
            raise RuntimeError("invalid_crawler_id")
        with self.connection.pool.connection() as connection:
            with connection.transaction():
                with connection.cursor() as cursor:
                    cursor.execute(
                        "SELECT pg_advisory_xact_lock(hashtextextended(%s,0))",
                        ("crawler:" + crawler_id,),
                    )
                    cursor.execute(
                        "SELECT * FROM crawler_leases WHERE crawler_id=%s FOR UPDATE",
                        (crawler_id,),
                    )
                    lease = cursor.fetchone()
                    if lease is not None:
                        age = (now - _utc(lease["updated_at"])).total_seconds()
                        if 0 <= age < CRAWLER_LEASE_ACTIVE_SECONDS:
                            return None
                    cursor.execute(
                        "SELECT payload_json FROM crawler_status WHERE crawler_id=%s FOR UPDATE",
                        (crawler_id,),
                    )
                    row = cursor.fetchone()
                    revision = 0 if row is None else _status(row["payload_json"]).revision
                    connected = replace(
                        initial_status,
                        run_id=run_id,
                        revision=revision + 1,
                        updated_at=now,
                    )
                    cursor.execute(
                        """INSERT INTO crawler_status(crawler_id,payload_json,updated_at)
                           VALUES (%s,%s::jsonb,%s)
                           ON CONFLICT(crawler_id) DO UPDATE SET
                             payload_json=EXCLUDED.payload_json,updated_at=EXCLUDED.updated_at""",
                        (crawler_id, json.dumps(_status_document(connected)), now),
                    )
                    cursor.execute(
                        """INSERT INTO crawler_leases
                           (crawler_id,run_id,lease_digest,connected_at,updated_at)
                           VALUES (%s,%s,%s,%s,%s)
                           ON CONFLICT(crawler_id) DO UPDATE SET
                             run_id=EXCLUDED.run_id,lease_digest=EXCLUDED.lease_digest,
                             connected_at=EXCLUDED.connected_at,updated_at=EXCLUDED.updated_at""",
                        (crawler_id, run_id, lease_digest, now, now),
                    )
                    cursor.execute(
                        """UPDATE crawler_actions SET status='queued',expected_revision=%s,
                           updated_at=%s WHERE status=%s""",
                        (connected.revision, now, f"claimed:{crawler_id}"),
                    )
                    return connected

    def publish_crawler_status_cas(
        self,
        crawler_id: str,
        run_id: str,
        lease_digest: str,
        expected_revision: int,
        status: CrawlerStatus,
        now: datetime,
    ) -> CrawlerStatus | None:
        with self.connection.pool.connection() as connection:
            with connection.transaction():
                with connection.cursor() as cursor:
                    cursor.execute(
                        "SELECT * FROM crawler_leases WHERE crawler_id=%s FOR UPDATE",
                        (crawler_id,),
                    )
                    lease = cursor.fetchone()
                    if (
                        lease is None
                        or lease["run_id"] != run_id
                        or lease["lease_digest"] != lease_digest
                    ):
                        return None
                    cursor.execute(
                        "SELECT payload_json FROM crawler_status WHERE crawler_id=%s FOR UPDATE",
                        (crawler_id,),
                    )
                    current = cursor.fetchone()
                    revision = 0 if current is None else _status(current["payload_json"]).revision
                    if (
                        revision != expected_revision
                        or status.revision != expected_revision + 1
                        or status.run_id != run_id
                    ):
                        return None
                    updated = replace(status, updated_at=now)
                    cursor.execute(
                        """UPDATE crawler_status SET payload_json=%s::jsonb,updated_at=%s
                           WHERE crawler_id=%s""",
                        (json.dumps(_status_document(updated)), now, crawler_id),
                    )
                    cursor.execute(
                        "UPDATE crawler_leases SET updated_at=%s WHERE crawler_id=%s",
                        (now, crawler_id),
                    )
                    cursor.execute(
                        """UPDATE crawler_actions SET status='rejected',updated_at=%s
                           WHERE status='queued' AND expected_revision<%s""",
                        (now, updated.revision),
                    )
                    return updated

    def enqueue_crawler_action(self, action: CrawlerAction) -> CrawlerAction | None:
        with self.connection.pool.connection() as connection:
            with connection.transaction():
                with connection.cursor() as cursor:
                    cursor.execute(
                        "SELECT * FROM crawler_actions WHERE action_id=%s FOR UPDATE",
                        (action.action_id,),
                    )
                    existing = cursor.fetchone()
                    if existing is not None:
                        return _action(existing)
                    cursor.execute(
                        "SELECT payload_json FROM crawler_status WHERE crawler_id='primary' FOR UPDATE"
                    )
                    status = cursor.fetchone()
                    if status is None or _status(status["payload_json"]).revision != action.expected_revision:
                        return None
                    cursor.execute(
                        """INSERT INTO crawler_actions
                           (action_id,kind,expected_revision,status,requested_by_user_id,
                            created_at,updated_at)
                           VALUES (%s,%s,%s,%s,%s,%s,%s)""",
                        (
                            action.action_id,
                            action.kind,
                            action.expected_revision,
                            action.status,
                            action.requested_by_user_id,
                            action.created_at,
                            action.updated_at,
                        ),
                    )
                    return action

    def claim_crawler_action(
        self,
        crawler_id: str,
        run_id: str,
        lease_digest: str,
        current_revision: int,
        now: datetime,
    ) -> CrawlerAction | None:
        with self.connection.pool.connection() as connection:
            with connection.transaction():
                with connection.cursor() as cursor:
                    cursor.execute(
                        "SELECT * FROM crawler_leases WHERE crawler_id=%s FOR UPDATE",
                        (crawler_id,),
                    )
                    lease = cursor.fetchone()
                    if (
                        lease is None
                        or lease["run_id"] != run_id
                        or lease["lease_digest"] != lease_digest
                    ):
                        return None
                    cursor.execute(
                        "SELECT payload_json FROM crawler_status WHERE crawler_id=%s FOR UPDATE",
                        (crawler_id,),
                    )
                    status_row = cursor.fetchone()
                    if status_row is None:
                        return None
                    status = _status(status_row["payload_json"])
                    if status.run_id != run_id or status.revision != current_revision:
                        return None
                    cursor.execute(
                        """SELECT * FROM crawler_actions
                           WHERE status='queued' AND expected_revision=%s
                           ORDER BY created_at,action_id LIMIT 1 FOR UPDATE SKIP LOCKED""",
                        (current_revision,),
                    )
                    row = cursor.fetchone()
                    if row is None:
                        return None
                    action = _action(row)
                    cursor.execute(
                        "UPDATE crawler_actions SET status=%s,updated_at=%s WHERE action_id=%s",
                        (f"claimed:{crawler_id}", now, action.action_id),
                    )
                    return replace(
                        action,
                        status="claimed",
                        claimed_by=crawler_id,
                        updated_at=now,
                    )

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
        if outcome not in {"applied", "rejected", "failed"}:
            return None
        with self.connection.pool.connection() as connection:
            with connection.transaction():
                with connection.cursor() as cursor:
                    cursor.execute(
                        "SELECT * FROM crawler_leases WHERE crawler_id=%s FOR UPDATE",
                        (crawler_id,),
                    )
                    lease = cursor.fetchone()
                    if (
                        lease is None
                        or lease["run_id"] != run_id
                        or lease["lease_digest"] != lease_digest
                    ):
                        return None
                    cursor.execute(
                        "SELECT payload_json FROM crawler_status WHERE crawler_id=%s FOR UPDATE",
                        (crawler_id,),
                    )
                    status_row = cursor.fetchone()
                    if status_row is None:
                        return None
                    status = _status(status_row["payload_json"])
                    if status.run_id != run_id or status.revision != current_revision:
                        return None
                    cursor.execute(
                        "SELECT * FROM crawler_actions WHERE action_id=%s FOR UPDATE",
                        (action_id,),
                    )
                    row = cursor.fetchone()
                    if row is None:
                        return None
                    action = _action(row)
                    if action.status != "claimed" or action.claimed_by != crawler_id:
                        return None
                    cursor.execute(
                        "UPDATE crawler_actions SET status=%s,updated_at=%s WHERE action_id=%s",
                        (outcome, now, action_id),
                    )
                    return replace(
                        action,
                        status=outcome,
                        updated_at=now,
                        completed_at=now,
                    )

    def list_crawler_actions(self, limit: int) -> list[CrawlerAction]:
        return [
            _action(row)
            for row in self._rows(
                "SELECT * FROM crawler_actions ORDER BY created_at DESC,action_id DESC LIMIT %s",
                (limit,),
            )
        ]

    def consume_rate_limit(
        self, key: str, limit: int, window_seconds: int, now: datetime
    ) -> bool:
        window = int(now.timestamp()) // window_seconds
        expires_at = datetime.fromtimestamp((window + 1) * window_seconds, tz=UTC)
        rows = self._rows(
            """INSERT INTO rate_limits(limit_key,window_id,request_count,expires_at)
               VALUES (%s,%s,1,%s)
               ON CONFLICT(limit_key,window_id) DO UPDATE
                 SET request_count=rate_limits.request_count+1,
                     expires_at=EXCLUDED.expires_at
                 WHERE rate_limits.request_count<%s
               RETURNING request_count""",
            (key, window, expires_at, limit),
        )
        return bool(rows)
