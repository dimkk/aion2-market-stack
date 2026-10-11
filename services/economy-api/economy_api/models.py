from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal

Role = Literal["registered", "approved", "admin"]
VALID_ROLES: frozenset[str] = frozenset({"registered", "approved", "admin"})


@dataclass(frozen=True, slots=True)
class User:
    username: str
    user_id: str
    password_hash: str
    role: Role
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class Session:
    session_hash: str
    username: str
    csrf_hash: str
    created_at: datetime
    expires_at: datetime


@dataclass(frozen=True, slots=True)
class DesktopSyncToken:
    token_id: str
    token_digest: str
    owner_username: str
    scope: str
    label: str | None
    created_at: datetime
    expires_at: datetime
    revoked_at: datetime | None


@dataclass(frozen=True, slots=True)
class FarmSession:
    owner_username: str
    session_id: str
    client_session_id: str
    payload: dict[str, Any]
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class AuditEvent:
    event_id: str
    occurred_at: datetime
    kind: str
    actor_user_id: str | None
    target_user_id: str | None
    request_id: str
    outcome: str


@dataclass(frozen=True, slots=True)
class CrawlerAction:
    action_id: str
    kind: str
    expected_revision: int
    status: str
    requested_by_user_id: str
    created_at: datetime
    updated_at: datetime
    claimed_by: str | None = None
    completed_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class CrawlerStatus:
    run_id: str | None
    status: str
    phase: str
    revision: int
    updated_at: datetime | None
    started_at: datetime | None
    elapsed_seconds: int
    total_items: int
    processed_items: int
    successful_requests: int
    failed_requests: int
    retried_requests: int
    queue_depth: int
    checkpoint: dict[str, Any] | None
    errors: tuple[dict[str, Any], ...]
