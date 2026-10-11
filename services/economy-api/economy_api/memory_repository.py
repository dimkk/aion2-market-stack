from __future__ import annotations

import threading
from dataclasses import replace
from datetime import datetime

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

CRAWLER_LEASE_ACTIVE_SECONDS = 420


class MemoryRepository:
    """Thread-safe test/development repository with production-equivalent rules."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self.users: dict[str, User] = {}
        self.sessions: dict[str, Session] = {}
        self.desktop_sync_tokens: dict[str, DesktopSyncToken] = {}
        self.farm_sessions: dict[tuple[str, str], FarmSession] = {}
        self.audit_events: list[AuditEvent] = []
        self.crawler_status: CrawlerStatus | None = None
        self.crawler_lease: tuple[str, str] | None = None
        self.crawler_lease_updated_at: datetime | None = None
        self.crawler_actions: dict[str, CrawlerAction] = {}
        self.rate_limits: dict[tuple[str, int], int] = {}

    def create_user(self, user: User) -> bool:
        with self._lock:
            if user.username in self.users:
                return False
            self.users[user.username] = user
            return True

    def get_user(self, username: str) -> User | None:
        with self._lock:
            return self.users.get(username)

    def list_users(self, limit: int) -> list[User]:
        with self._lock:
            return sorted(self.users.values(), key=lambda item: item.created_at)[:limit]

    def set_user_role(self, username: str, role: Role, now: datetime) -> User | None:
        with self._lock:
            user = self.users.get(username)
            if user is None:
                return None
            updated = replace(user, role=role, updated_at=now)
            self.users[username] = updated
            return updated

    def set_user_password_hash(
        self, username: str, password_hash: str, now: datetime
    ) -> User | None:
        with self._lock:
            user = self.users.get(username)
            if user is None:
                return None
            updated = replace(user, password_hash=password_hash, updated_at=now)
            self.users[username] = updated
            return updated

    def create_session(self, session: Session) -> None:
        with self._lock:
            self.sessions[session.session_hash] = session

    def get_session(self, session_hash: str) -> Session | None:
        with self._lock:
            return self.sessions.get(session_hash)

    def delete_session(self, session_hash: str) -> None:
        with self._lock:
            self.sessions.pop(session_hash, None)

    def create_desktop_sync_token(self, token: DesktopSyncToken) -> None:
        with self._lock:
            if token.token_id in self.desktop_sync_tokens:
                raise RuntimeError("desktop_sync_token_id_collision")
            self.desktop_sync_tokens[token.token_id] = token

    def get_desktop_sync_token(self, token_id: str) -> DesktopSyncToken | None:
        with self._lock:
            return self.desktop_sync_tokens.get(token_id)

    def list_desktop_sync_tokens(
        self, owner: str, limit: int
    ) -> list[DesktopSyncToken]:
        with self._lock:
            return sorted(
                (
                    token
                    for token in self.desktop_sync_tokens.values()
                    if token.owner_username == owner
                ),
                key=lambda token: (token.created_at, token.token_id),
                reverse=True,
            )[:limit]

    def revoke_desktop_sync_token(
        self, owner: str, token_id: str, now: datetime
    ) -> bool:
        with self._lock:
            token = self.desktop_sync_tokens.get(token_id)
            if token is None or token.owner_username != owner:
                return False
            if token.revoked_at is None:
                self.desktop_sync_tokens[token_id] = replace(token, revoked_at=now)
            return True

    def upsert_farm_session(
        self, session: FarmSession, max_sessions: int
    ) -> FarmSession | None:
        with self._lock:
            for key, current in list(self.farm_sessions.items()):
                if (
                    current.owner_username == session.owner_username
                    and current.client_session_id == session.client_session_id
                    and current.session_id != session.session_id
                ):
                    session = replace(
                        session,
                        session_id=current.session_id,
                        created_at=current.created_at,
                    )
                    del self.farm_sessions[key]
                    break
            identity = (session.owner_username, session.session_id)
            if identity not in self.farm_sessions and sum(
                1 for row in self.farm_sessions.values()
                if row.owner_username == session.owner_username
            ) >= max_sessions:
                return None
            self.farm_sessions[identity] = session
            return session

    def count_farm_sessions(self, owner: str) -> int:
        with self._lock:
            return sum(
                1 for row in self.farm_sessions.values() if row.owner_username == owner
            )

    def get_farm_session(self, owner: str, session_id: str) -> FarmSession | None:
        with self._lock:
            return self.farm_sessions.get((owner, session_id))

    def list_farm_sessions(
        self, owner: str | None, limit: int, cursor: str | None
    ) -> tuple[list[FarmSession], str | None]:
        with self._lock:
            rows = [
                row
                for row in self.farm_sessions.values()
                if owner is None or row.owner_username == owner
            ]
            rows.sort(
                key=lambda row: (row.owner_username, row.session_id), reverse=True
            )
            if cursor:
                if owner is None:
                    try:
                        cursor_owner, cursor_session = cursor.split(":", 1)
                    except ValueError:
                        rows = []
                    else:
                        rows = [
                            row for row in rows
                            if (row.owner_username, row.session_id)
                            < (cursor_owner, cursor_session)
                        ]
                else:
                    rows = [row for row in rows if row.session_id < cursor]
            page = rows[:limit]
            next_cursor = None
            if len(rows) > limit:
                last = page[-1]
                next_cursor = (
                    f"{last.owner_username}:{last.session_id}"
                    if owner is None else last.session_id
                )
            return page, next_cursor

    def delete_farm_session(self, owner: str, session_id: str) -> bool:
        with self._lock:
            return self.farm_sessions.pop((owner, session_id), None) is not None

    def append_audit(self, event: AuditEvent) -> None:
        with self._lock:
            self.audit_events.append(event)

    def get_crawler_status(self) -> CrawlerStatus | None:
        with self._lock:
            return self.crawler_status

    def publish_crawler_status_cas(
        self,
        crawler_id: str,
        run_id: str,
        lease_digest: str,
        expected_revision: int,
        status: CrawlerStatus,
        now: datetime,
    ) -> CrawlerStatus | None:
        if crawler_id != "primary":
            return None
        with self._lock:
            current_revision = self.crawler_status.revision if self.crawler_status else 0
            if (
                self.crawler_lease != (run_id, lease_digest)
                or current_revision != expected_revision
                or status.revision != expected_revision + 1
                or status.run_id != run_id
            ):
                return None
            # Lease liveness is server-authoritative. A worker may report an
            # arbitrary wall clock, so never use its timestamp for fencing.
            status = replace(status, updated_at=now)
            self.crawler_status = status
            self.crawler_lease_updated_at = now
            for action_id, action in list(self.crawler_actions.items()):
                if (
                    action.status == "queued"
                    and action.expected_revision < status.revision
                ):
                    self.crawler_actions[action_id] = replace(
                        action,
                        status="rejected",
                        updated_at=status.updated_at or action.updated_at,
                        completed_at=status.updated_at or action.updated_at,
                    )
            return status

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
        with self._lock:
            if (
                self.crawler_lease is not None
                and self.crawler_lease_updated_at is not None
                and 0
                <= (now - self.crawler_lease_updated_at).total_seconds()
                < CRAWLER_LEASE_ACTIVE_SECONDS
            ):
                return None
            revision = (self.crawler_status.revision if self.crawler_status else 0) + 1
            connected = replace(
                initial_status,
                run_id=run_id,
                revision=revision,
                updated_at=now,
            )
            self.crawler_status = connected
            self.crawler_lease = (run_id, lease_digest)
            self.crawler_lease_updated_at = now
            for action_id, action in list(self.crawler_actions.items()):
                if action.status == "claimed":
                    self.crawler_actions[action_id] = replace(
                        action,
                        status="queued",
                        expected_revision=revision,
                        claimed_by=None,
                        updated_at=now,
                    )
            return connected

    def enqueue_crawler_action(self, action: CrawlerAction) -> CrawlerAction | None:
        with self._lock:
            existing = self.crawler_actions.get(action.action_id)
            if existing is not None:
                return existing
            if (
                self.crawler_status is None
                or self.crawler_status.revision != action.expected_revision
            ):
                return None
            self.crawler_actions[action.action_id] = action
            return action

    def claim_crawler_action(
        self,
        crawler_id: str,
        run_id: str,
        lease_digest: str,
        current_revision: int,
        now: datetime,
    ) -> CrawlerAction | None:
        if crawler_id != "primary":
            return None
        with self._lock:
            if (
                self.crawler_lease != (run_id, lease_digest)
                or self.crawler_status is None
                or self.crawler_status.run_id != run_id
                or self.crawler_status.revision != current_revision
            ):
                return None
            candidates = sorted(
                (
                    action
                    for action in self.crawler_actions.values()
                    if action.status == "queued"
                    and action.expected_revision == current_revision
                ),
                key=lambda action: (action.created_at, action.action_id),
            )
            if not candidates:
                return None
            claimed = replace(
                candidates[0],
                status="claimed",
                claimed_by=crawler_id,
                updated_at=now,
            )
            self.crawler_actions[claimed.action_id] = claimed
            return claimed

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
        if crawler_id != "primary" or outcome not in {"applied", "rejected", "failed"}:
            return None
        with self._lock:
            action = self.crawler_actions.get(action_id)
            if (
                self.crawler_lease != (run_id, lease_digest)
                or self.crawler_status is None
                or self.crawler_status.run_id != run_id
                or self.crawler_status.revision != current_revision
                or action is None
                or action.status != "claimed"
                or action.claimed_by != crawler_id
            ):
                return None
            completed = replace(
                action,
                status=outcome,
                updated_at=now,
                completed_at=now,
            )
            self.crawler_actions[action_id] = completed
            return completed

    def list_crawler_actions(self, limit: int) -> list[CrawlerAction]:
        with self._lock:
            return sorted(
                self.crawler_actions.values(),
                key=lambda item: (item.created_at, item.action_id),
                reverse=True,
            )[:limit]

    def consume_rate_limit(
        self, key: str, limit: int, window_seconds: int, now: datetime
    ) -> bool:
        window = int(now.timestamp()) // window_seconds
        bucket = (key, window)
        with self._lock:
            count = self.rate_limits.get(bucket, 0)
            if count >= limit:
                return False
            self.rate_limits[bucket] = count + 1
            if len(self.rate_limits) > 10_000:
                self.rate_limits = {
                    item: value for item, value in self.rate_limits.items() if item[1] >= window - 2
                }
            return True
