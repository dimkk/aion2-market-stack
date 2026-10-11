from __future__ import annotations

from datetime import datetime
from typing import Protocol

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


class Repository(Protocol):
    def create_user(self, user: User) -> bool: ...

    def get_user(self, username: str) -> User | None: ...

    def list_users(self, limit: int) -> list[User]: ...

    def set_user_role(self, username: str, role: Role, now: datetime) -> User | None: ...

    def set_user_password_hash(
        self, username: str, password_hash: str, now: datetime
    ) -> User | None: ...

    def create_session(self, session: Session) -> None: ...

    def get_session(self, session_hash: str) -> Session | None: ...

    def delete_session(self, session_hash: str) -> None: ...

    def create_desktop_sync_token(self, token: DesktopSyncToken) -> None: ...

    def get_desktop_sync_token(self, token_id: str) -> DesktopSyncToken | None: ...

    def list_desktop_sync_tokens(
        self, owner: str, limit: int
    ) -> list[DesktopSyncToken]: ...

    def revoke_desktop_sync_token(
        self, owner: str, token_id: str, now: datetime
    ) -> bool: ...

    def upsert_farm_session(
        self, session: FarmSession, max_sessions: int
    ) -> FarmSession | None: ...

    def count_farm_sessions(self, owner: str) -> int: ...

    def get_farm_session(self, owner: str, session_id: str) -> FarmSession | None: ...

    def list_farm_sessions(
        self, owner: str | None, limit: int, cursor: str | None
    ) -> tuple[list[FarmSession], str | None]: ...

    def delete_farm_session(self, owner: str, session_id: str) -> bool: ...

    def append_audit(self, event: AuditEvent) -> None: ...

    def get_crawler_status(self) -> CrawlerStatus | None: ...

    def connect_crawler(
        self,
        crawler_id: str,
        run_id: str,
        lease_digest: str,
        initial_status: CrawlerStatus,
        now: datetime,
    ) -> CrawlerStatus | None: ...

    def publish_crawler_status_cas(
        self,
        crawler_id: str,
        run_id: str,
        lease_digest: str,
        expected_revision: int,
        status: CrawlerStatus,
        now: datetime,
    ) -> CrawlerStatus | None: ...

    def enqueue_crawler_action(self, action: CrawlerAction) -> CrawlerAction | None: ...

    def claim_crawler_action(
        self,
        crawler_id: str,
        run_id: str,
        lease_digest: str,
        current_revision: int,
        now: datetime,
    ) -> CrawlerAction | None: ...

    def complete_crawler_action(
        self,
        crawler_id: str,
        run_id: str,
        lease_digest: str,
        action_id: str,
        current_revision: int,
        outcome: str,
        now: datetime,
    ) -> CrawlerAction | None: ...

    def list_crawler_actions(self, limit: int) -> list[CrawlerAction]: ...

    def consume_rate_limit(
        self, key: str, limit: int, window_seconds: int, now: datetime
    ) -> bool: ...
