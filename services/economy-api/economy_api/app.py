from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import math
import re
import secrets
import uuid
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from typing import Any, Callable
from urllib.parse import urlsplit

from .models import (
    AuditEvent,
    CrawlerAction,
    CrawlerStatus,
    DesktopSyncToken,
    FarmSession,
    User,
)
from .repository import Repository
from .security import (
    CSRF_COOKIE,
    SESSION_COOKIE,
    SecurityError,
    create_session,
    create_desktop_sync_token,
    csrf_set_cookie,
    desktop_sync_token_id,
    decode_session_cookie,
    digest_token,
    expired_cookies,
    hash_password,
    password_hash_needs_rehash,
    normalize_username,
    parse_cookie_header,
    pseudonym,
    require_session_secret,
    session_set_cookie,
    validate_password,
    verify_login_password,
)
from .snapshot_ingest import (
    MAX_COMMIT_DOCUMENT_BYTES,
    MAX_UPLOAD_DOCUMENT_BYTES,
    MAX_WIRE_BYTES,
    RECEIPT_SCHEMA,
    REVISION_RE,
    SHA256_RE,
    SnapshotIngestError,
    canonical_json,
    decode_request_document,
    sha256_bytes,
    validate_commit,
    validate_view_upload,
    view_upload_idempotency_key,
)
from .store import EconomyStore

_LOG = logging.getLogger("aion2_economy_api")
_SAFE_LOG_FIELDS = frozenset({"event", "request_id", "actor", "outcome", "status"})
_ID_RE = re.compile(r"^[A-Za-z0-9._:-]{1,96}$")
_PAGE_CURSOR_RE = re.compile(r"^[A-Za-z0-9._:-]{1,161}$")
_ITEM_ID_RE = re.compile(r"^[A-Za-z0-9._:-]{1,48}$")
_SEGMENT_RE = re.compile(r"^[a-z0-9_-]{1,48}$")
_PAYLOAD_ID_RE = re.compile(r"^[a-f0-9]{64}$")
_ECONOMY_VIEWS = frozenset({"items", "movers", "servers", "craft", "farm-value"})
_CRAWLER_ACTIONS = frozenset({"pause", "resume", "retry"})
_FARM_ACTIVITY_KINDS = frozenset({"spot", "dungeon", "mob", "other"})
_SYNC_TOKEN_LABEL_RE = re.compile(r"^[A-Za-z0-9 _.-]{1,64}$")
_CRAWLER_LEASE_RE = re.compile(r"^a2cl_[A-Za-z0-9_-]{43}$")
_FARM_SESSION_NAMESPACE = uuid.UUID("25bc7b96-7da3-4c79-bc1a-8126798af365")
MAX_FARM_SESSIONS_PER_USER = 1_000
CRAWLER_LEASE_ACTIVE_SECONDS = 420


def _safe_diagnostic_text(value: str) -> str:
    value = re.sub(r"\bBearer\s+[A-Za-z0-9._~+/=-]+", "Bearer [redacted]", value, flags=re.I)
    value = re.sub(
        r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{8,}\b",
        "[redacted-jwt]",
        value,
    )
    value = re.sub(
        r"\ba2(?:fs|farm|cl)_[A-Za-z0-9_-]{20,}\b",
        "[redacted-token]",
        value,
        flags=re.I,
    )
    return re.sub(
        r"\b(authorization|cookie|password|secret|token|jwt)\s*[:=]\s*[^\s,;]+",
        r"\1=[redacted]",
        value,
        flags=re.I,
    )[:240]


@dataclass(frozen=True, slots=True)
class AppConfig:
    # Extra exact HTTPS origins are optional. The normal deployment is same-origin
    # and validates Origin against the gateway-supplied Host header, so creating
    # the gateway does not require an insecure two-pass origin bootstrap.
    allowed_origins: tuple[str, ...] = ()
    session_ttl_seconds: int = 43_200
    max_body_bytes: int = 65_536
    max_farm_loot_rows: int = 500
    desktop_token_ttl_seconds: int = 604_800
    desktop_token_max_ttl_seconds: int = 2_592_000
    registration_enabled: bool = True
    write_fenced: bool = False
    api_release_sha256: str | None = None


@dataclass(frozen=True, slots=True)
class Principal:
    user: User
    session_hash: str
    csrf_hash: str


class ApiError(Exception):
    def __init__(self, status: int, code: str) -> None:
        super().__init__(code)
        self.status = status
        self.code = code


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _json_default(value: object) -> str:
    if isinstance(value, datetime):
        return value.astimezone(UTC).isoformat().replace("+00:00", "Z")
    raise TypeError(type(value).__name__)


class ApiApplication:
    def __init__(
        self,
        repository: Repository,
        economy_store: EconomyStore,
        session_secret: bytes,
        config: AppConfig,
        *,
        clock: Callable[[], datetime] = _utcnow,
    ) -> None:
        self.repository = repository
        self.economy_store = economy_store
        self.secret = require_session_secret(session_secret)
        self.config = config
        self.clock = clock

    def handle(self, event: dict[str, Any], _context: object = None) -> dict[str, Any]:
        request_id = self._request_id(event)
        try:
            method, path, headers, body, ip = self._request(event)
            response = self._dispatch(method, path, headers, body, ip, request_id)
            self._safe_log(event="request", request_id=request_id, status=response["statusCode"])
            return response
        except ApiError as exc:
            self._safe_log(
                event="request_rejected",
                request_id=request_id,
                status=exc.status,
                outcome=exc.code,
            )
            return self._response(exc.status, {"error": exc.code}, request_id=request_id)
        except Exception:
            self._safe_log(
                event="request_failed",
                request_id=request_id,
                status=500,
                outcome="internal_error",
            )
            return self._response(500, {"error": "internal_error"}, request_id=request_id)

    def _request(
        self, event: dict[str, Any]
    ) -> tuple[str, str, dict[str, str], bytes, str]:
        context = event.get("requestContext") or {}
        http = context.get("http") or {}
        method = str(event.get("httpMethod") or http.get("method") or "GET").upper()
        path = str(event.get("path") or event.get("rawPath") or http.get("path") or "/")
        raw_headers = event.get("headers") or {}
        headers = {str(key).lower(): str(value) for key, value in raw_headers.items()}
        query = event.get("queryStringParameters") or {}
        if "segment" in query and "x-economy-segment" not in headers:
            headers["x-economy-segment"] = str(query["segment"])
        raw_body = event.get("body") or ""
        if event.get("isBase64Encoded"):
            try:
                body = base64.b64decode(raw_body, validate=True)
            except ValueError as exc:
                raise ApiError(400, "invalid_body") from exc
        else:
            body = str(raw_body).encode("utf-8")
        snapshot_ingest_route = path in {
            "/api/v1/crawler/economy/views",
            "/api/v1/crawler/economy/commit",
            "/api/v1/crawler/economy/status",
        }
        maximum_body_bytes = (
            MAX_WIRE_BYTES if snapshot_ingest_route else self.config.max_body_bytes
        )
        if len(body) > maximum_body_bytes:
            raise ApiError(413, "payload_too_large")
        identity = context.get("identity") or {}
        ip = str(identity.get("sourceIp") or http.get("sourceIp") or "unknown")
        return method, path.rstrip("/") or "/", headers, body, ip

    @staticmethod
    def _request_id(event: dict[str, Any]) -> str:
        context = event.get("requestContext") or {}
        value = str(context.get("requestId") or uuid.uuid4())
        return value[:128]

    def _dispatch(
        self,
        method: str,
        path: str,
        headers: dict[str, str],
        body: bytes,
        ip: str,
        request_id: str,
    ) -> dict[str, Any]:
        if path == "/api/v1/health" and method == "GET":
            return self._response(
                200,
                {
                    "status": "ok",
                    "apiRelease": self.config.api_release_sha256,
                },
                request_id=request_id,
            )

        if self.config.write_fenced and method not in {"GET", "HEAD", "OPTIONS"}:
            raise ApiError(503, "write_fenced")

        if path == "/api/v1/auth/register" and method == "POST":
            # Private pilot deployments are invite-only. Reject before origin,
            # rate-limit storage and password hashing so random internet traffic
            # cannot turn registration into a paid CPU/YDB amplification path.
            if not self.config.registration_enabled:
                raise ApiError(404, "not_found")
            self._require_origin(headers)
            self._rate("register", ip, 5, 300)
            return self._register(body, request_id)

        if path == "/api/v1/auth/login" and method == "POST":
            self._require_origin(headers)
            self._rate("login", ip, 10, 300)
            return self._login(body, request_id)

        if path == "/api/v1/desktop/farm/sessions" and method == "POST":
            # Desktop sync deliberately has no browser-cookie fallback.  The
            # credential is scoped to this single route and never authorizes
            # market reads, user administration or crawler controls.
            if headers.get("cookie"):
                raise ApiError(401, "desktop_token_required")
            self._rate("desktop_farm_ip", ip, 60, 60)
            principal, token = self._scoped_service_principal(
                headers, "farm:write", "desktop_token_required"
            )
            self._rate("desktop_farm_token", token.token_id, 30, 60)
            return self._write_desktop_farm(
                principal, body, request_id, headers.get("idempotency-key")
            )

        crawler_service_route = (
            path == "/api/v1/crawler/connect"
            or path == "/api/v1/crawler/heartbeat"
            or path == "/api/v1/crawler/actions/claim"
            or path == "/api/v1/crawler/economy/views"
            or path == "/api/v1/crawler/economy/commit"
            or path == "/api/v1/crawler/economy/status"
            or path == "/api/v1/crawler/economy/readback"
            or (path.startswith("/api/v1/crawler/actions/") and path.endswith("/complete"))
        )
        if crawler_service_route and method in {"GET", "POST"}:
            if headers.get("cookie"):
                raise ApiError(401, "crawler_token_required")
            self._rate("crawler_service_ip", ip, 120, 60)
            principal, token = self._scoped_service_principal(
                headers, "crawler:control", "crawler_token_required"
            )
            self._rate("crawler_service_token", token.token_id, 120, 60)
            if path == "/api/v1/crawler/economy/readback":
                if method != "GET":
                    raise ApiError(404, "not_found")
                return self._crawler_economy_readback(request_id)
            if method != "POST":
                raise ApiError(404, "not_found")
            if path == "/api/v1/crawler/connect":
                return self._crawler_connect(principal, token, body, request_id)
            if path == "/api/v1/crawler/heartbeat":
                return self._crawler_heartbeat(principal, token, body, request_id)
            if path == "/api/v1/crawler/actions/claim":
                return self._crawler_claim(principal, token, body, request_id)
            if path == "/api/v1/crawler/economy/views":
                return self._crawler_economy_view(
                    principal, body, headers, request_id
                )
            if path == "/api/v1/crawler/economy/commit":
                return self._crawler_economy_commit(
                    principal, body, headers, request_id
                )
            if path == "/api/v1/crawler/economy/status":
                return self._crawler_economy_status(
                    principal, body, headers, request_id
                )
            action_id = path.removeprefix("/api/v1/crawler/actions/").removesuffix(
                "/complete"
            )
            return self._crawler_complete(
                principal, token, action_id, body, request_id
            )

        principal = self._principal(headers)
        self._rate("authenticated", principal.user.user_id, 240, 60)

        if path == "/api/v1/auth/me" and method == "GET":
            return self._response(200, {"user": self._public_user(principal.user)}, request_id)

        if path == "/api/v1/auth/logout" and method == "POST":
            self._require_csrf(headers, principal)
            self.repository.delete_session(principal.session_hash)
            self._audit("logout", principal.user, None, request_id, "ok")
            return self._response(204, None, request_id, cookies=expired_cookies())

        if path == "/api/v1/market/snapshot" and method == "GET":
            self._require_role(principal, "approved")
            segment = self._segment(event_query=None, headers=headers)
            return self._economy("market", segment, request_id)

        if path.startswith("/api/v1/economy/") and method == "GET":
            self._require_role(principal, "approved")
            view = path.removeprefix("/api/v1/economy/")
            if view not in _ECONOMY_VIEWS:
                raise ApiError(404, "not_found")
            segment = self._segment(event_query=None, headers=headers)
            return self._economy(view, segment, request_id)

        if path == "/api/v1/farm/sessions":
            self._require_role(principal, "approved")
            if method == "GET":
                return self._list_farm(principal.user.username, headers, request_id)
            if method == "POST":
                self._require_csrf(headers, principal)
                self._rate("farm_write", principal.user.user_id, 30, 60)
                return self._write_farm(
                    principal, None, body, request_id, headers.get("idempotency-key")
                )

        if path == "/api/v1/farm/sync-tokens":
            self._require_role(principal, "approved")
            if method == "GET":
                tokens = [
                    token
                    for token in self.repository.list_desktop_sync_tokens(
                        principal.user.username, 100
                    )
                    if token.scope == "farm:write"
                ]
                return self._response(
                    200,
                    {"tokens": [self._desktop_token_json(token) for token in tokens]},
                    request_id,
                )
            if method == "POST":
                self._require_csrf(headers, principal)
                self._rate("desktop_token_issue", principal.user.user_id, 5, 3600)
                return self._issue_desktop_sync_token(principal, body, request_id)

        if path.startswith("/api/v1/farm/sync-tokens/") and method == "DELETE":
            self._require_role(principal, "approved")
            self._require_csrf(headers, principal)
            token_id = path.removeprefix("/api/v1/farm/sync-tokens/")
            if not re.fullmatch(r"[a-f0-9]{32}", token_id):
                raise ApiError(404, "not_found")
            if not self.repository.revoke_desktop_sync_token(
                principal.user.username, token_id, self.clock()
            ):
                raise ApiError(404, "not_found")
            self._audit("desktop_sync_token_revoke", principal.user, None, request_id, "ok")
            return self._response(204, None, request_id)

        if path.startswith("/api/v1/farm/sessions/"):
            self._require_role(principal, "approved")
            session_id = path.removeprefix("/api/v1/farm/sessions/")
            if not _ID_RE.fullmatch(session_id):
                raise ApiError(404, "not_found")
            if method == "GET":
                return self._get_farm(principal.user.username, session_id, request_id)
            if method == "PUT":
                self._require_csrf(headers, principal)
                self._rate("farm_write", principal.user.user_id, 30, 60)
                return self._write_farm(
                    principal, session_id, body, request_id, headers.get("idempotency-key")
                )
            if method == "DELETE":
                self._require_csrf(headers, principal)
                self._rate("farm_write", principal.user.user_id, 30, 60)
                return self._delete_farm(principal, session_id, request_id)

        if path == "/api/v1/admin/users" and method == "GET":
            self._require_role(principal, "admin")
            users = [self._public_user(item) for item in self.repository.list_users(500)]
            return self._response(200, {"users": users}, request_id)

        if path.startswith("/api/v1/admin/users/") and path.endswith("/role") and method == "POST":
            self._require_role(principal, "admin")
            self._require_csrf(headers, principal)
            self._rate("admin_write", principal.user.user_id, 30, 60)
            username = path.removeprefix("/api/v1/admin/users/").removesuffix("/role")
            return self._set_role(principal, username, body, request_id)

        if path == "/api/v1/admin/farm-sessions" and method == "GET":
            self._require_role(principal, "admin")
            return self._list_farm(None, headers, request_id)

        if path == "/api/v1/admin/crawler/status" and method == "GET":
            self._require_role(principal, "admin")
            return self._crawler_status(request_id)

        if path == "/api/v1/admin/crawler/tokens":
            self._require_role(principal, "admin")
            if method == "GET":
                tokens = [
                    token
                    for token in self.repository.list_desktop_sync_tokens(
                        principal.user.username, 100
                    )
                    if token.scope == "crawler:control"
                ]
                return self._response(
                    200,
                    {"tokens": [self._desktop_token_json(token) for token in tokens]},
                    request_id,
                )
            if method == "POST":
                self._require_csrf(headers, principal)
                self._rate("crawler_token_issue", principal.user.user_id, 5, 3600)
                return self._issue_crawler_token(principal, body, request_id)

        if path.startswith("/api/v1/admin/crawler/tokens/") and method == "DELETE":
            self._require_role(principal, "admin")
            self._require_csrf(headers, principal)
            token_id = path.removeprefix("/api/v1/admin/crawler/tokens/")
            if not re.fullmatch(r"[a-f0-9]{32}", token_id):
                raise ApiError(404, "not_found")
            token = self.repository.get_desktop_sync_token(token_id)
            if token is None or token.scope != "crawler:control":
                raise ApiError(404, "not_found")
            if not self.repository.revoke_desktop_sync_token(
                principal.user.username, token_id, self.clock()
            ):
                raise ApiError(404, "not_found")
            self._audit("crawler_token_revoke", principal.user, None, request_id, "ok")
            return self._response(204, None, request_id)

        if path == "/api/v1/admin/crawler/actions":
            self._require_role(principal, "admin")
            if method == "GET":
                actions = [
                    self._crawler_action_json(item)
                    for item in self.repository.list_crawler_actions(100)
                ]
                return self._response(200, {"actions": actions}, request_id)
            if method == "POST":
                self._require_csrf(headers, principal)
                self._rate("admin_write", principal.user.user_id, 30, 60)
                return self._enqueue_crawler_action(principal, body, request_id)

        raise ApiError(404, "not_found")

    def _body_json(self, body: bytes) -> dict[str, Any]:
        try:
            value = json.loads(body or b"{}")
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise ApiError(400, "invalid_json") from exc
        if not isinstance(value, dict):
            raise ApiError(400, "invalid_json")
        return value

    def _register(self, body: bytes, request_id: str) -> dict[str, Any]:
        data = self._body_json(body)
        try:
            username = normalize_username(data.get("username"))
            password = validate_password(data.get("password"))
        except SecurityError as exc:
            raise ApiError(400, str(exc)) from exc
        now = self.clock()
        try:
            password_hash = hash_password(password)
        except Exception as exc:
            self._safe_log(
                event="dependency_failed",
                request_id=request_id,
                status=503,
                outcome="password_hash_unavailable",
            )
            raise ApiError(503, "password_hash_unavailable") from exc
        user = User(
            username=username,
            user_id=str(uuid.uuid4()),
            password_hash=password_hash,
            role="registered",
            created_at=now,
            updated_at=now,
        )
        try:
            created = self.repository.create_user(user)
        except Exception as exc:
            self._safe_log(
                event="dependency_failed",
                request_id=request_id,
                status=503,
                outcome="database_unavailable",
            )
            raise ApiError(503, "database_unavailable") from exc
        if created:
            self._audit("register", user, user, request_id, "registered")
        # Deliberately identical response for existing and newly-created usernames.
        return self._response(202, {"status": "registration_received"}, request_id)

    def _login(self, body: bytes, request_id: str) -> dict[str, Any]:
        data = self._body_json(body)
        try:
            username = normalize_username(data.get("username"))
        except SecurityError:
            username = "invalid-user"
        password = data.get("password")
        user = self.repository.get_user(username)
        valid = verify_login_password(password, user.password_hash if user else None)
        if not user or not valid:
            raise ApiError(401, "invalid_credentials")
        if password_hash_needs_rehash(user.password_hash):
            try:
                upgraded = self.repository.set_user_password_hash(
                    user.username, hash_password(password), self.clock()
                )
            except Exception as exc:
                self._safe_log(
                    event="dependency_failed", request_id=request_id, status=503,
                    outcome="password_rehash_unavailable",
                )
                raise ApiError(503, "password_rehash_unavailable") from exc
            if upgraded is None:
                raise ApiError(503, "password_rehash_unavailable")
            user = upgraded
        created = create_session(
            user.username,
            self.secret,
            now=self.clock(),
            ttl=timedelta(seconds=self.config.session_ttl_seconds),
        )
        self.repository.create_session(created.record)
        self._audit("login", user, None, request_id, "ok")
        cookies = [
            session_set_cookie(created.cookie_value, self.config.session_ttl_seconds),
            csrf_set_cookie(created.csrf_value, self.config.session_ttl_seconds),
        ]
        return self._response(200, {"user": self._public_user(user)}, request_id, cookies)

    def _principal(self, headers: dict[str, str]) -> Principal:
        cookies = parse_cookie_header(headers.get("cookie"))
        cookie_value = cookies.get(SESSION_COOKIE, "")
        session_hash = decode_session_cookie(cookie_value, self.secret)
        if not session_hash:
            raise ApiError(401, "authentication_required")
        session = self.repository.get_session(session_hash)
        if not session or session.expires_at <= self.clock():
            if session:
                self.repository.delete_session(session_hash)
            raise ApiError(401, "authentication_required")
        user = self.repository.get_user(session.username)
        if not user:
            self.repository.delete_session(session_hash)
            raise ApiError(401, "authentication_required")
        return Principal(user=user, session_hash=session_hash, csrf_hash=session.csrf_hash)

    def _scoped_service_principal(
        self, headers: dict[str, str], expected_scope: str, error_code: str
    ) -> tuple[Principal, DesktopSyncToken]:
        authorization = headers.get("authorization", "")
        if not authorization.startswith("Bearer ") or len(authorization) > 256:
            raise ApiError(401, error_code)
        token_value = authorization[7:]
        token_id = desktop_sync_token_id(token_value)
        if token_id is None:
            raise ApiError(401, error_code)
        token = self.repository.get_desktop_sync_token(token_id)
        now = self.clock()
        if (
            token is None
            or token.scope != expected_scope
            or token.revoked_at is not None
            or token.expires_at <= now
            or not hmac.compare_digest(
                token.token_digest, digest_token(token_value, self.secret)
            )
        ):
            raise ApiError(401, error_code)
        user = self.repository.get_user(token.owner_username)
        if user is None:
            raise ApiError(401, error_code)
        principal = Principal(user=user, session_hash="", csrf_hash="")
        self._require_role(
            principal, "admin" if expected_scope == "crawler:control" else "approved"
        )
        return principal, token

    def _issue_desktop_sync_token(
        self, principal: Principal, body: bytes, request_id: str
    ) -> dict[str, Any]:
        data = self._body_json(body)
        if not set(data).issubset({"label", "expires_in_seconds"}):
            raise ApiError(400, "invalid_desktop_token_request")
        label = data.get("label")
        if label is not None and (
            not isinstance(label, str) or not _SYNC_TOKEN_LABEL_RE.fullmatch(label)
        ):
            raise ApiError(400, "invalid_desktop_token_request")
        expires_in = data.get(
            "expires_in_seconds", self.config.desktop_token_ttl_seconds
        )
        if (
            not isinstance(expires_in, int)
            or isinstance(expires_in, bool)
            or expires_in < 300
            or expires_in > self.config.desktop_token_max_ttl_seconds
        ):
            raise ApiError(400, "invalid_desktop_token_request")
        issued = create_desktop_sync_token(
            principal.user.username,
            self.secret,
            now=self.clock(),
            ttl=timedelta(seconds=expires_in),
            label=label,
            scope="farm:write",
        )
        self.repository.create_desktop_sync_token(issued.record)
        self._audit("desktop_sync_token_issue", principal.user, None, request_id, "ok")
        return self._response(
            201,
            {
                "token": issued.token_value,
                "token_meta": self._desktop_token_json(issued.record),
            },
            request_id,
            extra_headers={"Cache-Control": "no-store"},
        )

    def _issue_crawler_token(
        self, principal: Principal, body: bytes, request_id: str
    ) -> dict[str, Any]:
        data = self._body_json(body)
        if not set(data).issubset({"crawler_id", "expires_in_seconds"}):
            raise ApiError(400, "invalid_crawler_token_request")
        crawler_id = data.get("crawler_id")
        if crawler_id != "primary":
            raise ApiError(400, "invalid_crawler_token_request")
        expires_in = data.get(
            "expires_in_seconds", self.config.desktop_token_ttl_seconds
        )
        if (
            not isinstance(expires_in, int)
            or isinstance(expires_in, bool)
            or expires_in < 300
            or expires_in > self.config.desktop_token_max_ttl_seconds
        ):
            raise ApiError(400, "invalid_crawler_token_request")
        issued = create_desktop_sync_token(
            principal.user.username,
            self.secret,
            now=self.clock(),
            ttl=timedelta(seconds=expires_in),
            label=crawler_id,
            scope="crawler:control",
        )
        self.repository.create_desktop_sync_token(issued.record)
        self._audit("crawler_token_issue", principal.user, None, request_id, "ok")
        return self._response(
            201,
            {
                "token": issued.token_value,
                "token_meta": self._desktop_token_json(issued.record),
            },
            request_id,
            extra_headers={"Cache-Control": "no-store"},
        )

    def _require_origin(self, headers: dict[str, str]) -> None:
        origin = headers.get("origin", "")
        try:
            parsed = urlsplit(origin)
            valid_shape = (
                parsed.scheme == "https"
                and bool(parsed.netloc)
                and parsed.username is None
                and parsed.password is None
                and parsed.path in {"", "/"}
                and not parsed.query
                and not parsed.fragment
            )
        except ValueError:
            valid_shape = False
            parsed = urlsplit("")
        if not valid_shape:
            raise ApiError(403, "origin_rejected")
        canonical = f"https://{parsed.netloc.lower()}"
        host = headers.get("host", "").strip().lower()
        same_origin = bool(host) and parsed.netloc.lower() == host
        extra_allowed = canonical in {
            value.rstrip("/").lower() for value in self.config.allowed_origins
        }
        if not same_origin and not extra_allowed:
            raise ApiError(403, "origin_rejected")

    def _require_csrf(self, headers: dict[str, str], principal: Principal) -> None:
        self._require_origin(headers)
        cookies = parse_cookie_header(headers.get("cookie"))
        from_cookie = cookies.get(CSRF_COOKIE, "")
        from_header = headers.get("x-csrf-token", "")
        if not from_cookie or from_cookie != from_header:
            raise ApiError(403, "csrf_rejected")
        if digest_token(from_header, self.secret) != principal.csrf_hash:
            raise ApiError(403, "csrf_rejected")

    @staticmethod
    def _require_role(principal: Principal, required: str) -> None:
        rank = {"registered": 1, "approved": 2, "admin": 3}
        if rank.get(principal.user.role, 0) < rank[required]:
            raise ApiError(403, "approval_required")

    def _rate(self, scope: str, identity: str, limit: int, seconds: int) -> None:
        key = f"{scope}:{pseudonym(identity, self.secret)}"
        if not self.repository.consume_rate_limit(key, limit, seconds, self.clock()):
            raise ApiError(429, "rate_limited")

    def _economy(self, view: str, segment: str, request_id: str) -> dict[str, Any]:
        value = self.economy_store.read(view, segment)
        if value is None:
            raise ApiError(404, "snapshot_not_available")
        return self._response(
            200,
            value,
            request_id,
            extra_headers={"Cache-Control": "private, max-age=60, must-revalidate"},
        )

    @staticmethod
    def _segment(event_query: object, headers: dict[str, str]) -> str:
        # API Gateway forwards a validated shard selector as a header.  The
        # default keeps the first deployment useful without trusting paths.
        segment = headers.get("x-economy-segment", "default").lower()
        if not _SEGMENT_RE.fullmatch(segment):
            raise ApiError(400, "invalid_segment")
        return segment

    def _list_farm(
        self, owner: str | None, headers: dict[str, str], request_id: str
    ) -> dict[str, Any]:
        try:
            limit = min(max(int(headers.get("x-page-limit", "50")), 1), 100)
        except ValueError as exc:
            raise ApiError(400, "invalid_pagination") from exc
        cursor = headers.get("x-page-cursor") or None
        if cursor and not _PAGE_CURSOR_RE.fullmatch(cursor):
            raise ApiError(400, "invalid_pagination")
        rows, next_cursor = self.repository.list_farm_sessions(owner, limit, cursor)
        return self._response(
            200,
            {"sessions": [self._farm_json(row) for row in rows], "next_cursor": next_cursor},
            request_id,
        )

    def _get_farm(self, owner: str, session_id: str, request_id: str) -> dict[str, Any]:
        row = self.repository.get_farm_session(owner, session_id)
        if row is None:
            raise ApiError(404, "not_found")
        return self._response(200, {"session": self._farm_json(row)}, request_id)

    def _write_farm(
        self,
        principal: Principal,
        session_id: str | None,
        body: bytes,
        request_id: str,
        idempotency_key: str | None,
    ) -> dict[str, Any]:
        row, is_update = self._store_farm(
            principal.user, session_id, body, idempotency_key
        )
        self._audit("farm_upsert", principal.user, None, request_id, "ok")
        return self._response(
            200 if is_update else 201,
            {"session": self._farm_json(row)},
            request_id,
        )

    def _write_desktop_farm(
        self,
        principal: Principal,
        body: bytes,
        request_id: str,
        idempotency_key: str | None,
    ) -> dict[str, Any]:
        row, _ = self._store_farm(principal.user, None, body, idempotency_key)
        payload_id = row.payload.get("payload_id")
        if not isinstance(payload_id, str):
            raise ApiError(400, "invalid_farm_session")
        self._audit("desktop_farm_sync", principal.user, None, request_id, "ok")
        return self._response(
            200,
            {
                "schema": "a2.farm.sync-receipt.v1",
                "payload_id": payload_id,
                "accepted": True,
                "receipt_id": row.session_id,
            },
            request_id,
            extra_headers={"Cache-Control": "no-store"},
        )

    def _store_farm(
        self,
        user: User,
        session_id: str | None,
        body: bytes,
        idempotency_key: str | None,
    ) -> tuple[FarmSession, bool]:
        data = self._validate_farm(self._body_json(body))
        if data["schema"] == "a2.farm.session-export.v1" and (
            idempotency_key is None
            or idempotency_key != data.get("payload_id")
        ):
            raise ApiError(400, "invalid_idempotency_key")
        now = self.clock()
        is_update = session_id is not None
        if session_id:
            current = self.repository.get_farm_session(user.username, session_id)
            if current is None:
                raise ApiError(404, "not_found")
            created_at = current.created_at
        else:
            session_id = str(
                uuid.uuid5(
                    _FARM_SESSION_NAMESPACE,
                    f"{user.username}\x00{data['client_session_id']}",
                )
            )
            current = self.repository.get_farm_session(user.username, session_id)
            if current is None:
                created_at = now
            else:
                created_at = current.created_at
                is_update = True
        row = FarmSession(
            owner_username=user.username,
            session_id=session_id,
            client_session_id=data["client_session_id"],
            payload=data,
            created_at=created_at,
            updated_at=now,
        )
        stored = self.repository.upsert_farm_session(row, MAX_FARM_SESSIONS_PER_USER)
        if stored is None:
            raise ApiError(409, "farm_session_limit_reached")
        row = stored
        return row, is_update

    def _delete_farm(
        self, principal: Principal, session_id: str, request_id: str
    ) -> dict[str, Any]:
        if not self.repository.delete_farm_session(principal.user.username, session_id):
            raise ApiError(404, "not_found")
        self._audit("farm_delete", principal.user, None, request_id, "ok")
        return self._response(204, None, request_id)

    def _validate_farm(self, value: dict[str, Any]) -> dict[str, Any]:
        if value.get("schema") == "a2.farm.session-export.v1":
            if set(value) != {"schema", "payload_id", "session"}:
                raise ApiError(400, "invalid_farm_session")
            payload_id_value = value.get("payload_id")
            nested = value.get("session")
            required_session = {
                "session_id",
                "activity",
                "started_at_ms",
                "stopped_at_ms",
                "observed_direct_kina",
                "estimated_loot_value",
                "estimated_total_value",
                "event_count",
                "loot",
            }
            if (
                not isinstance(payload_id_value, str)
                or not _PAYLOAD_ID_RE.fullmatch(payload_id_value)
                or not isinstance(nested, dict)
                or set(nested) != required_session
            ):
                raise ApiError(400, "invalid_farm_session")
            ordered_session: dict[str, Any] = {
                field: nested[field]
                for field in (
                    "session_id", "activity", "started_at_ms", "stopped_at_ms",
                    "observed_direct_kina", "estimated_loot_value",
                    "estimated_total_value", "event_count",
                )
            }
            raw_loot = nested.get("loot")
            if not isinstance(raw_loot, list):
                raise ApiError(400, "invalid_farm_session")
            required_loot = {
                "item_id", "quantity", "unit_price", "market_server_id",
                "price_observed_at_ms", "price_source", "estimated_value",
            }
            ordered_loot: list[dict[str, Any]] = []
            for item in raw_loot:
                if not isinstance(item, dict) or set(item) != required_loot:
                    raise ApiError(400, "invalid_farm_session")
                ordered_loot.append({field: item[field] for field in (
                    "item_id", "quantity", "unit_price", "market_server_id",
                    "price_observed_at_ms", "price_source", "estimated_value",
                )})
            ordered_session["loot"] = ordered_loot
            canonical = json.dumps(
                {"schema": "a2.farm.session-export.v1", "session": ordered_session},
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode("utf-8")
            expected_payload_id = hashlib.sha256(canonical).hexdigest()
            if not hmac.compare_digest(payload_id_value, expected_payload_id):
                raise ApiError(400, "farm_payload_hash_mismatch")
            value = {
                "schema": "a2.farm.session-export.v1",
                "payload_id": payload_id_value,
                **nested,
            }
        schema = value.get("schema") or value.get("schema_version")
        if schema is not None and schema != "a2.farm.session-export.v1":
            raise ApiError(400, "invalid_farm_session")
        payload_id = value.get("payload_id")
        if schema == "a2.farm.session-export.v1" and (
            not isinstance(payload_id, str) or not _PAYLOAD_ID_RE.fullmatch(payload_id)
        ):
            raise ApiError(400, "invalid_farm_session")
        source_session_id = value.get("session_id")
        activity = value.get("activity")
        if activity is not None:
            if not isinstance(activity, dict) or set(activity) != {"kind", "label"}:
                raise ApiError(400, "invalid_farm_session")
            activity_kind = activity.get("kind")
            activity_label = activity.get("label")
            if activity_kind not in _FARM_ACTIVITY_KINDS:
                raise ApiError(400, "invalid_farm_session")
            if activity_label is not None and (
                not isinstance(activity_label, str)
                or not activity_label
                or len(activity_label.encode("utf-8")) > 80
                or activity_label.strip() != activity_label
                or any(ord(char) < 32 or 127 <= ord(char) <= 159 for char in activity_label)
            ):
                raise ApiError(400, "invalid_farm_session")
        export_v1 = schema == "a2.farm.session-export.v1" or any(
            key in value
            for key in (
                "payload_id",
                "observed_direct_kina",
                "estimated_loot_value",
                "estimated_total_value",
                "started_at_ms",
                "stopped_at_ms",
            )
        )
        client_id = value.get("client_session_id") or payload_id or source_session_id
        if not isinstance(client_id, str) or not _ID_RE.fullmatch(client_id):
            raise ApiError(400, "invalid_farm_session")
        for identifier in (payload_id, source_session_id):
            if identifier is not None and (
                not isinstance(identifier, str) or not _ID_RE.fullmatch(identifier)
            ):
                raise ApiError(400, "invalid_farm_session")

        duration = value.get("duration_seconds")
        started_ms = value.get("started_at_ms")
        stopped_ms = value.get("stopped_at_ms")
        if started_ms is not None or stopped_ms is not None:
            if (
                not isinstance(started_ms, int)
                or isinstance(started_ms, bool)
                or not isinstance(stopped_ms, int)
                or isinstance(stopped_ms, bool)
                or started_ms < 0
                or stopped_ms < started_ms
                or stopped_ms > 4_102_444_800_000
            ):
                raise ApiError(400, "invalid_farm_session")
            derived_duration = (stopped_ms - started_ms) / 1000
            if duration is None:
                duration = derived_duration
            elif (
                not isinstance(duration, (int, float))
                or isinstance(duration, bool)
                or not math.isfinite(float(duration))
                or abs(float(duration) - derived_duration) > 1
            ):
                raise ApiError(400, "invalid_farm_session")

        direct = value.get("observed_direct_kina")
        legacy_total = value.get("total_kina")
        if direct is None:
            direct = legacy_total if legacy_total is not None else 0
        if (
            not isinstance(duration, (int, float))
            or isinstance(duration, bool)
            or not math.isfinite(float(duration))
            or duration <= 0
            or duration > 604_800
            or not isinstance(direct, int)
            or isinstance(direct, bool)
            or abs(direct) > 10**15
        ):
            raise ApiError(400, "invalid_farm_session")
        loot = value.get("loot", [])
        if not isinstance(loot, list) or len(loot) > self.config.max_farm_loot_rows:
            raise ApiError(400, "invalid_farm_session")
        clean_loot = []
        for item in loot:
            if not isinstance(item, dict):
                raise ApiError(400, "invalid_farm_session")
            if export_v1 and not set(item).issubset(
                {
                    "item_id",
                    "quantity",
                    "unit_price",
                    "market_server_id",
                    "price_observed_at_ms",
                    "price_source",
                    "estimated_value",
                }
            ):
                raise ApiError(400, "invalid_farm_session")
            item_id = str(item.get("item_id", ""))
            quantity = item.get("quantity")
            unit_price = item.get("unit_price", item.get("estimated_unit_price"))
            estimated_value = item.get("estimated_value")
            market_server_id = item.get("market_server_id")
            observed_at = item.get("price_observed_at_ms")
            price_source = item.get("price_source")
            if (
                not _ITEM_ID_RE.fullmatch(item_id)
                or not isinstance(quantity, int)
                or isinstance(quantity, bool)
                or quantity < 1
                or quantity > 10**9
            ):
                raise ApiError(400, "invalid_farm_session")
            if unit_price is not None and (
                not isinstance(unit_price, int)
                or isinstance(unit_price, bool)
                or unit_price < 0
                or unit_price > 10**15
            ):
                raise ApiError(400, "invalid_farm_session")
            calculated_value = (
                quantity * unit_price if unit_price is not None else None
            )
            if unit_price is None and estimated_value is not None:
                raise ApiError(400, "invalid_farm_session")
            if (
                unit_price is not None
                and estimated_value is not None
                and estimated_value != calculated_value
            ):
                raise ApiError(400, "invalid_farm_session")
            if observed_at is not None and (
                not isinstance(observed_at, int)
                or isinstance(observed_at, bool)
                or observed_at < 0
                or observed_at > 4_102_444_800_000
            ):
                raise ApiError(400, "invalid_farm_session")
            if market_server_id is not None and (
                not isinstance(market_server_id, int)
                or isinstance(market_server_id, bool)
                or market_server_id < 1
                or market_server_id > 10**12
            ):
                raise ApiError(400, "invalid_farm_session")
            if price_source is not None and (
                not isinstance(price_source, str)
                or not _ID_RE.fullmatch(price_source)
            ):
                raise ApiError(400, "invalid_farm_session")
            clean_item: dict[str, Any] = {
                "item_id": item_id,
                "quantity": quantity,
                "unit_price": unit_price,
                "estimated_unit_price": unit_price,
                "estimated_value": calculated_value,
                "market_server_id": market_server_id,
            }
            if observed_at is not None:
                clean_item["price_observed_at_ms"] = observed_at
            if price_source is not None:
                clean_item["price_source"] = price_source
            clean_loot.append(clean_item)

        calculated_loot = sum(
            item["estimated_value"] or 0 for item in clean_loot
        )
        loot_value = value.get(
            "estimated_loot_value", calculated_loot if export_v1 else 0
        )
        total = value.get("estimated_total_value")
        if total is None:
            total = (
                direct + loot_value
                if export_v1
                else legacy_total if legacy_total is not None else direct
            )
        if (
            not isinstance(loot_value, int)
            or isinstance(loot_value, bool)
            or loot_value < 0
            or loot_value > 10**15
            or (export_v1 and loot_value != calculated_loot)
            or not isinstance(total, int)
            or isinstance(total, bool)
            or abs(total) > 10**15
            or (export_v1 and total != direct + loot_value)
        ):
            raise ApiError(400, "invalid_farm_session")
        event_count = value.get("event_count", 0)
        if (
            not isinstance(event_count, int)
            or isinstance(event_count, bool)
            or event_count < 0
            or event_count > 10**9
        ):
            raise ApiError(400, "invalid_farm_session")
        clean: dict[str, Any] = {
            "schema": (
                "a2.farm.session-export.v1" if export_v1 else "a2.farm.session.v0"
            ),
            "client_session_id": client_id,
            "duration_seconds": float(duration),
            "observed_direct_kina": direct,
            "estimated_loot_value": loot_value,
            "estimated_total_value": total,
            "total_kina": total,
            "kina_per_second": round(total / float(duration), 4),
            "direct_kina_per_second": round(direct / float(duration), 4),
            "event_count": event_count,
            "loot": clean_loot,
            "activity": activity,
        }
        if payload_id is not None:
            clean["payload_id"] = payload_id
        if source_session_id is not None:
            clean["source_session_id"] = source_session_id
        if started_ms is not None:
            clean["started_at_ms"] = started_ms
            clean["stopped_at_ms"] = stopped_ms
        for field in (
            "started_at",
            "stopped_at",
            "ended_at",
            "server",
            "faction",
            "location",
        ):
            item = value.get(field)
            if item is not None:
                if not isinstance(item, str) or len(item) > 96:
                    raise ApiError(400, "invalid_farm_session")
                clean[field] = item
        return clean

    def _set_role(
        self,
        principal: Principal,
        raw_username: str,
        body: bytes,
        request_id: str,
    ) -> dict[str, Any]:
        try:
            username = normalize_username(raw_username)
        except SecurityError as exc:
            raise ApiError(404, "not_found") from exc
        target = self.repository.get_user(username)
        if target is None:
            raise ApiError(404, "not_found")
        if target.role == "admin" or target.username == principal.user.username:
            raise ApiError(409, "admin_role_protected")
        role = self._body_json(body).get("role")
        if role not in {"registered", "approved"}:
            raise ApiError(400, "invalid_role")
        updated = self.repository.set_user_role(username, role, self.clock())
        assert updated is not None
        self._audit("role_change", principal.user, target, request_id, role)
        return self._response(200, {"user": self._public_user(updated)}, request_id)

    def _crawler_status(self, request_id: str) -> dict[str, Any]:
        status = self.repository.get_crawler_status()
        read_standalone = getattr(self.economy_store, "read_crawler_status", None)
        if callable(read_standalone):
            try:
                standalone = read_standalone(self.clock())
            except Exception as exc:
                # The mutable legacy control plane remains a bounded fallback
                # while a current worker lease is healthy.  A malformed or
                # temporarily unavailable object must not hide a live worker,
                # but it is fatal when no such fallback exists.
                if status is None or not self._crawler_status_is_fresh(status):
                    raise ApiError(503, "crawler_status_unavailable") from exc
                standalone = None
            if standalone is not None:
                if self.config.api_release_sha256 is not None:
                    standalone = {
                        **standalone,
                        "apiRelease": self.config.api_release_sha256,
                    }
                return self._response(200, standalone, request_id)
        if status is not None and self._crawler_status_is_fresh(status):
            payload = self._crawler_status_payload(status)
        else:
            payload = self._crawler_status_payload(None)
        if self.config.api_release_sha256 is not None:
            payload["apiRelease"] = self.config.api_release_sha256
        return self._response(200, payload, request_id)

    def _crawler_status_is_fresh(self, status: CrawlerStatus) -> bool:
        if status.updated_at is None:
            return False
        age = (self.clock() - status.updated_at).total_seconds()
        return 0 <= age < CRAWLER_LEASE_ACTIVE_SECONDS

    def _crawler_connect(
        self,
        principal: Principal,
        token: DesktopSyncToken,
        body: bytes,
        request_id: str,
    ) -> dict[str, Any]:
        value = self._body_json(body)
        if set(value) != {"crawler_id", "runId", "status"}:
            raise ApiError(400, "invalid_crawler_connect")
        crawler_id = value.get("crawler_id")
        run_id = value.get("runId")
        raw_status = value.get("status")
        if (
            crawler_id != token.label
            or crawler_id != "primary"
            or not isinstance(run_id, str)
            or not _ID_RE.fullmatch(run_id)
            or not isinstance(raw_status, dict)
            or "revision" in raw_status
            or "runId" in raw_status
        ):
            raise ApiError(400, "invalid_crawler_connect")
        initial_status = self._validate_crawler_status(
            {**raw_status, "runId": run_id, "revision": 0}
        )
        now = self.clock()
        lease_id = f"a2cl_{secrets.token_urlsafe(32)}"
        connected = self.repository.connect_crawler(
            crawler_id,
            run_id,
            digest_token(lease_id, self.secret),
            replace(initial_status, run_id=run_id, updated_at=now),
            now,
        )
        if connected is None:
            raise ApiError(409, "crawler_already_connected")
        self._audit("crawler_connect", principal.user, None, request_id, "ok")
        return self._response(
            200,
            {"leaseId": lease_id, "status": self._crawler_status_json(connected)},
            request_id,
            extra_headers={"Cache-Control": "no-store"},
        )

    def _crawler_heartbeat(
        self,
        principal: Principal,
        token: DesktopSyncToken,
        body: bytes,
        request_id: str,
    ) -> dict[str, Any]:
        value = self._body_json(body)
        if set(value) != {
            "crawler_id",
            "runId",
            "leaseId",
            "expectedRevision",
            "status",
        }:
            raise ApiError(400, "invalid_crawler_heartbeat")
        crawler_id = value.get("crawler_id")
        run_id = value.get("runId")
        lease_id = value.get("leaseId")
        expected_revision = value.get("expectedRevision")
        if (
            crawler_id != token.label
            or crawler_id != "primary"
            or not isinstance(run_id, str)
            or not _ID_RE.fullmatch(run_id)
            or not isinstance(lease_id, str)
            or not _CRAWLER_LEASE_RE.fullmatch(lease_id)
            or not isinstance(expected_revision, int)
            or isinstance(expected_revision, bool)
            or expected_revision < 0
        ):
            raise ApiError(400, "invalid_crawler_heartbeat")
        status_value = value.get("status")
        if not isinstance(status_value, dict):
            raise ApiError(400, "invalid_crawler_heartbeat")
        status = self._validate_crawler_status(status_value)
        if status.run_id != run_id or status.revision != expected_revision + 1:
            raise ApiError(409, "crawler_revision_conflict")
        now = self.clock()
        published = self.repository.publish_crawler_status_cas(
            crawler_id,
            run_id,
            digest_token(lease_id, self.secret),
            expected_revision,
            status,
            now,
        )
        if published is None:
            raise ApiError(409, "crawler_revision_conflict")
        return self._response(
            200, {"status": self._crawler_status_json(published)}, request_id
        )

    def _crawler_claim(
        self,
        principal: Principal,
        token: DesktopSyncToken,
        body: bytes,
        request_id: str,
    ) -> dict[str, Any]:
        crawler_id, run_id, lease_digest, revision = self._crawler_service_identity(
            token, body
        )
        current = self.repository.get_crawler_status()
        if current is None or current.run_id != run_id or current.revision != revision:
            raise ApiError(409, "crawler_revision_conflict")
        claimed = self.repository.claim_crawler_action(
            crawler_id, run_id, lease_digest, revision, self.clock()
        )
        latest = self.repository.get_crawler_status()
        if latest is None or latest.run_id != run_id or latest.revision != revision:
            raise ApiError(409, "crawler_revision_conflict")
        if claimed is not None:
            self._audit("crawler_action_claim", principal.user, None, request_id, "ok")
        return self._response(
            200,
            {
                "action": self._crawler_action_json(claimed) if claimed else None,
                "status": self._crawler_status_json(latest),
            },
            request_id,
        )

    def _crawler_complete(
        self,
        principal: Principal,
        token: DesktopSyncToken,
        action_id: str,
        body: bytes,
        request_id: str,
    ) -> dict[str, Any]:
        if not _ID_RE.fullmatch(action_id):
            raise ApiError(404, "not_found")
        value = self._body_json(body)
        if set(value) != {
            "crawler_id",
            "runId",
            "leaseId",
            "currentRevision",
            "outcome",
        }:
            raise ApiError(400, "invalid_crawler_completion")
        crawler_id = value.get("crawler_id")
        run_id = value.get("runId")
        lease_id = value.get("leaseId")
        revision = value.get("currentRevision")
        outcome = value.get("outcome")
        if (
            crawler_id != token.label
            or crawler_id != "primary"
            or not isinstance(run_id, str)
            or not _ID_RE.fullmatch(run_id)
            or not isinstance(lease_id, str)
            or not _CRAWLER_LEASE_RE.fullmatch(lease_id)
            or not isinstance(revision, int)
            or isinstance(revision, bool)
            or revision < 0
            or outcome not in {"applied", "rejected", "failed"}
        ):
            raise ApiError(400, "invalid_crawler_completion")
        completed = self.repository.complete_crawler_action(
            crawler_id,
            run_id,
            digest_token(lease_id, self.secret),
            action_id,
            revision,
            outcome,
            self.clock(),
        )
        if completed is None:
            raise ApiError(409, "crawler_action_conflict")
        latest = self.repository.get_crawler_status()
        if latest is None or latest.run_id != run_id or latest.revision != revision:
            raise ApiError(409, "crawler_revision_conflict")
        self._audit("crawler_action_complete", principal.user, None, request_id, outcome)
        return self._response(
            200,
            {
                "action": self._crawler_action_json(completed),
                "status": self._crawler_status_json(latest),
            },
            request_id,
        )

    def _crawler_economy_view(
        self,
        principal: Principal,
        body: bytes,
        headers: dict[str, str],
        request_id: str,
    ) -> dict[str, Any]:
        try:
            value = decode_request_document(
                body,
                content_encoding=headers.get("content-encoding", ""),
                maximum_document_bytes=MAX_UPLOAD_DOCUMENT_BYTES,
            )
            revision, name, encoded, digest = validate_view_upload(value)
        except SnapshotIngestError as exc:
            raise ApiError(400, str(exc)) from exc
        expected_idempotency = view_upload_idempotency_key(revision, name, digest)
        if headers.get("idempotency-key") != expected_idempotency:
            raise ApiError(400, "snapshot_idempotency_key_invalid")
        try:
            self.economy_store.stage_view(revision, name, encoded, digest)
            current_revision = self.economy_store.current_revision()
        except ValueError as exc:
            code = str(exc)
            status = 409 if "conflict" in code else 400
            raise ApiError(status, code) from exc
        except Exception as exc:
            raise ApiError(503, "snapshot_store_unavailable") from exc
        self._audit("economy_view_stage", principal.user, None, request_id, "ok")
        return self._response(
            200,
            {
                "status": "view_verified",
                "revision": revision,
                "name": name,
                "sha256": digest,
                "currentRevision": current_revision,
                "raw_exposed": False,
            },
            request_id,
        )

    def _crawler_economy_readback(self, request_id: str) -> dict[str, Any]:
        try:
            current_revision = self.economy_store.current_revision()
            read_standalone = getattr(self.economy_store, "read_crawler_status", None)
            crawler_status = (
                read_standalone(self.clock()) if callable(read_standalone) else None
            )
        except Exception as exc:
            raise ApiError(503, "snapshot_store_unavailable") from exc
        return self._response(
            200,
            {
                "schema": "aion2.economy.crawler-readback/v1",
                "currentRevision": current_revision,
                "crawlerStatus": crawler_status,
                "apiRelease": self.config.api_release_sha256,
            },
            request_id,
        )

    def _crawler_economy_status(
        self,
        principal: Principal,
        body: bytes,
        headers: dict[str, str],
        request_id: str,
    ) -> dict[str, Any]:
        try:
            value = decode_request_document(
                body,
                content_encoding=headers.get("content-encoding", ""),
                maximum_document_bytes=MAX_COMMIT_DOCUMENT_BYTES,
            )
            if set(value) != {"schema", "revision", "sha256", "document"}:
                raise ApiError(400, "crawler_status_upload_invalid")
            if value.get("schema") != "aion2.economy.crawler-status-upload/v1":
                raise ApiError(400, "crawler_status_upload_invalid")
            revision = value.get("revision")
            digest = value.get("sha256")
            document = value.get("document")
            if not isinstance(document, dict):
                raise ApiError(400, "crawler_status_upload_invalid")
            encoded = canonical_json(document)
            if (
                not isinstance(revision, str)
                or REVISION_RE.fullmatch(revision) is None
                or not isinstance(digest, str)
                or SHA256_RE.fullmatch(digest) is None
                or sha256_bytes(encoded) != digest
                or headers.get("idempotency-key") != digest
            ):
                raise ApiError(400, "crawler_status_upload_invalid")
            write_status = getattr(self.economy_store, "write_crawler_status", None)
            if not callable(write_status):
                raise ApiError(503, "crawler_status_store_unavailable")
            receipt = write_status(
                revision, encoded, digest, now=self.clock()
            )
        except ApiError:
            raise
        except ValueError as exc:
            code = str(exc)
            status = 409 if "conflict" in code or "superseded" in code else 400
            raise ApiError(status, code) from exc
        except Exception as exc:
            raise ApiError(503, "crawler_status_store_unavailable") from exc
        self._audit("crawler_status_publish", principal.user, None, request_id, "ok")
        return self._response(
            200,
            {
                "status": "crawler_status_verified",
                "revision": revision,
                "sha256": receipt["sha256"],
                "idempotent": receipt["idempotent"],
                "raw_exposed": False,
            },
            request_id,
        )

    def _crawler_economy_commit(
        self,
        principal: Principal,
        body: bytes,
        headers: dict[str, str],
        request_id: str,
    ) -> dict[str, Any]:
        try:
            value = decode_request_document(
                body,
                content_encoding=headers.get("content-encoding", ""),
                maximum_document_bytes=MAX_COMMIT_DOCUMENT_BYTES,
            )
            revision, expected, views, publication_digest = validate_commit(value)
        except SnapshotIngestError as exc:
            raise ApiError(400, str(exc)) from exc
        if headers.get("idempotency-key") != publication_digest:
            raise ApiError(400, "snapshot_idempotency_key_invalid")
        try:
            committed = self.economy_store.commit_views(
                revision,
                expected,
                views,
                publication_digest[:32],
            )
        except ValueError as exc:
            code = str(exc)
            status = 409 if "conflict" in code else 400
            raise ApiError(status, code) from exc
        except Exception as exc:
            raise ApiError(503, "snapshot_store_unavailable") from exc
        self._audit("economy_views_publish", principal.user, None, request_id, "ok")
        return self._response(
            200,
            {
                "schema": RECEIPT_SCHEMA,
                "status": "idempotent" if committed["idempotent"] else "published",
                "verified": True,
                "revision": revision,
                "currentRevision": revision,
                "views": committed["views"],
                "hashes": committed["hashes"],
                "publicationSha256": publication_digest,
                "credentials_exported": False,
                "raw_exposed": False,
            },
            request_id,
        )

    def _crawler_service_identity(
        self, token: DesktopSyncToken, body: bytes
    ) -> tuple[str, str, str, int]:
        value = self._body_json(body)
        if set(value) != {"crawler_id", "runId", "leaseId", "currentRevision"}:
            raise ApiError(400, "invalid_crawler_claim")
        crawler_id = value.get("crawler_id")
        run_id = value.get("runId")
        lease_id = value.get("leaseId")
        revision = value.get("currentRevision")
        if (
            crawler_id != token.label
            or crawler_id != "primary"
            or not isinstance(run_id, str)
            or not _ID_RE.fullmatch(run_id)
            or not isinstance(lease_id, str)
            or not _CRAWLER_LEASE_RE.fullmatch(lease_id)
            or not isinstance(revision, int)
            or isinstance(revision, bool)
            or revision < 0
        ):
            raise ApiError(400, "invalid_crawler_claim")
        return crawler_id, run_id, digest_token(lease_id, self.secret), revision

    @staticmethod
    def _validate_crawler_status(value: dict[str, Any]) -> CrawlerStatus:
        fields = {
            "runId",
            "status",
            "phase",
            "revision",
            "updatedAt",
            "startedAt",
            "elapsedSeconds",
            "totalItems",
            "processedItems",
            "successfulRequests",
            "failedRequests",
            "retriedRequests",
            "queueDepth",
            "checkpoint",
            "errors",
        }
        if set(value) != fields:
            raise ApiError(400, "invalid_crawler_status")
        run_id = value.get("runId")
        if run_id is not None and (
            not isinstance(run_id, str) or not _ID_RE.fullmatch(run_id)
        ):
            raise ApiError(400, "invalid_crawler_status")
        if value.get("status") not in {
            "starting",
            "running",
            "pausing",
            "paused",
            "retrying",
            "completed",
            "failed",
        }:
            raise ApiError(400, "invalid_crawler_status")
        if value.get("phase") not in {
            "idle",
            "session",
            "catalog",
            "queue",
            "scan",
            "items",
            "persist",
            "publish",
            "complete",
        }:
            raise ApiError(400, "invalid_crawler_status")

        def bounded_int(name: str) -> int:
            item = value.get(name)
            if (
                not isinstance(item, int)
                or isinstance(item, bool)
                or item < 0
                or item > 2**63 - 1
            ):
                raise ApiError(400, "invalid_crawler_status")
            return item

        def timestamp(name: str, *, optional: bool) -> datetime | None:
            item = value.get(name)
            if item is None and optional:
                return None
            if not isinstance(item, str) or len(item) > 40:
                raise ApiError(400, "invalid_crawler_status")
            try:
                parsed = datetime.fromisoformat(item.replace("Z", "+00:00"))
            except ValueError as exc:
                raise ApiError(400, "invalid_crawler_status") from exc
            if parsed.tzinfo is None:
                raise ApiError(400, "invalid_crawler_status")
            return parsed.astimezone(UTC)

        checkpoint = value.get("checkpoint")
        if checkpoint is not None:
            if (
                not isinstance(checkpoint, dict)
                or set(checkpoint) != {"cursor", "processedItems", "savedAt", "revision"}
                or not isinstance(checkpoint.get("cursor"), str)
                or len(checkpoint["cursor"]) > 128
                or not isinstance(checkpoint.get("savedAt"), str)
                or len(checkpoint["savedAt"]) > 40
            ):
                raise ApiError(400, "invalid_crawler_status")
            for name in ("processedItems", "revision"):
                item = checkpoint.get(name)
                if (
                    not isinstance(item, int)
                    or isinstance(item, bool)
                    or item < 0
                    or item > 2**63 - 1
                ):
                    raise ApiError(400, "invalid_crawler_status")
        errors = value.get("errors")
        if not isinstance(errors, list) or len(errors) > 20:
            raise ApiError(400, "invalid_crawler_status")
        safe_errors: list[dict[str, Any]] = []
        required_error = {"errorId", "at", "code", "errorClass", "message", "retryable"}
        for item in errors:
            if (
                not isinstance(item, dict)
                or not required_error.issubset(item)
                or not set(item).issubset(required_error | {"itemId", "attempt"})
                or not all(
                    isinstance(item.get(name), str)
                    for name in ("errorId", "at", "code", "errorClass", "message")
                )
                or len(item["errorId"]) > 96
                or len(item["at"]) > 40
                or len(item["code"]) > 64
                or len(item["errorClass"]) > 96
                or len(item["message"]) > 240
                or not isinstance(item.get("retryable"), bool)
                or not _ID_RE.fullmatch(item["errorId"])
                or not _ID_RE.fullmatch(item["code"])
                or not _ID_RE.fullmatch(item["errorClass"])
            ):
                raise ApiError(400, "invalid_crawler_status")
            safe = {name: item[name] for name in required_error}
            safe["message"] = _safe_diagnostic_text(item["message"])
            if "itemId" in item:
                if not isinstance(item["itemId"], str) or len(item["itemId"]) > 64:
                    raise ApiError(400, "invalid_crawler_status")
                safe["itemId"] = item["itemId"]
            if "attempt" in item:
                attempt = item["attempt"]
                if (
                    not isinstance(attempt, int)
                    or isinstance(attempt, bool)
                    or attempt < 0
                    or attempt > 1000
                ):
                    raise ApiError(400, "invalid_crawler_status")
                safe["attempt"] = attempt
            safe_errors.append(safe)
        return CrawlerStatus(
            run_id=run_id,
            status=value["status"],
            phase=value["phase"],
            revision=bounded_int("revision"),
            updated_at=timestamp("updatedAt", optional=False),
            started_at=timestamp("startedAt", optional=True),
            elapsed_seconds=bounded_int("elapsedSeconds"),
            total_items=bounded_int("totalItems"),
            processed_items=bounded_int("processedItems"),
            successful_requests=bounded_int("successfulRequests"),
            failed_requests=bounded_int("failedRequests"),
            retried_requests=bounded_int("retriedRequests"),
            queue_depth=bounded_int("queueDepth"),
            checkpoint=dict(checkpoint) if checkpoint is not None else None,
            errors=tuple(safe_errors),
        )

    def _enqueue_crawler_action(
        self, principal: Principal, body: bytes, request_id: str
    ) -> dict[str, Any]:
        value = self._body_json(body)
        action_id = value.get("idempotencyKey", value.get("idempotency_key"))
        kind = value.get("action", value.get("kind"))
        expected_revision = value.get("expectedRevision")
        if (
            not isinstance(action_id, str)
            or not _ID_RE.fullmatch(action_id)
            or kind not in _CRAWLER_ACTIONS
            or not isinstance(expected_revision, int)
            or isinstance(expected_revision, bool)
            or expected_revision < 0
            or expected_revision > 2**63 - 1
        ):
            raise ApiError(400, "invalid_crawler_action")
        status = self.repository.get_crawler_status()
        if status is None or not self._crawler_status_is_fresh(status):
            raise ApiError(409, "crawler_not_connected")
        if expected_revision != status.revision:
            raise ApiError(409, "crawler_revision_conflict")
        allowed_actions = {
            "running": frozenset({"pause"}),
            "paused": frozenset({"resume", "retry"}),
            "failed": frozenset({"retry"}),
        }
        if kind not in allowed_actions.get(status.status, frozenset()):
            raise ApiError(409, "crawler_action_not_allowed")
        now = self.clock()
        queued = self.repository.enqueue_crawler_action(
            CrawlerAction(
                action_id=action_id,
                kind=kind,
                expected_revision=expected_revision,
                status="queued",
                requested_by_user_id=principal.user.user_id,
                created_at=now,
                updated_at=now,
            )
        )
        if queued is None:
            raise ApiError(409, "crawler_revision_conflict")
        self._audit("crawler_action", principal.user, None, request_id, kind)
        status_json = self._crawler_status_payload(status)
        return self._response(
            202,
            {"status": status_json},
            request_id,
        )

    def _audit(
        self,
        kind: str,
        actor: User,
        target: User | None,
        request_id: str,
        outcome: str,
    ) -> None:
        event = AuditEvent(
            event_id=str(uuid.uuid4()),
            occurred_at=self.clock(),
            kind=kind,
            actor_user_id=actor.user_id,
            target_user_id=target.user_id if target else None,
            request_id=request_id,
            outcome=outcome,
        )
        self.repository.append_audit(event)
        self._safe_log(
            event=kind,
            request_id=request_id,
            actor=pseudonym(actor.user_id, self.secret),
            outcome=outcome,
        )

    @staticmethod
    def _public_user(user: User) -> dict[str, str]:
        return {"id": user.user_id, "username": user.username, "role": user.role}

    @staticmethod
    def _farm_json(row: FarmSession) -> dict[str, Any]:
        return {
            "id": row.session_id,
            "owner": row.owner_username,
            "created_at": row.created_at,
            "updated_at": row.updated_at,
            **row.payload,
        }

    @staticmethod
    def _desktop_token_json(token: DesktopSyncToken) -> dict[str, Any]:
        return {
            "id": token.token_id,
            "scope": token.scope,
            "label": token.label,
            "created_at": token.created_at,
            "expires_at": token.expires_at,
            "revoked_at": token.revoked_at,
        }

    @staticmethod
    def _crawler_action_json(row: CrawlerAction) -> dict[str, Any]:
        return {
            "id": row.action_id,
            "action": row.kind,
            "expectedRevision": row.expected_revision,
            "status": row.status,
            "claimedBy": row.claimed_by,
            "created_at": row.created_at,
            "updated_at": row.updated_at,
            "completedAt": row.completed_at,
        }

    @staticmethod
    def _crawler_status_json(row: CrawlerStatus) -> dict[str, Any]:
        return {
            "configured": True,
            "runId": row.run_id,
            "status": row.status,
            "phase": row.phase,
            "revision": row.revision,
            "updatedAt": row.updated_at,
            "startedAt": row.started_at,
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

    def _crawler_status_payload(self, row: CrawlerStatus | None) -> dict[str, Any]:
        if row is not None:
            return self._crawler_status_json(row)
        return {
            "configured": False,
            "runId": None,
            "status": "not_connected",
            "phase": "idle",
            "revision": 0,
            "updatedAt": None,
            "startedAt": None,
            "elapsedSeconds": 0,
            "totalItems": 0,
            "processedItems": 0,
            "successfulRequests": 0,
            "failedRequests": 0,
            "retriedRequests": 0,
            "queueDepth": len(self.repository.list_crawler_actions(100)),
            "checkpoint": None,
            "errors": [],
        }

    @staticmethod
    def _response(
        status: int,
        payload: object,
        request_id: str,
        cookies: list[str] | None = None,
        extra_headers: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        headers = {
            "Content-Type": "application/json; charset=utf-8",
            "X-Content-Type-Options": "nosniff",
            "Referrer-Policy": "no-referrer",
            "Strict-Transport-Security": "max-age=31536000; includeSubDomains",
            "Permissions-Policy": "camera=(), microphone=(), geolocation=()",
            "Cache-Control": "no-store",
            "X-Request-Id": request_id,
        }
        if extra_headers:
            headers.update(extra_headers)
        response: dict[str, Any] = {
            "statusCode": status,
            "headers": headers,
            "body": (
                ""
                if payload is None
                else json.dumps(payload, default=_json_default, separators=(",", ":"))
            ),
        }
        if cookies:
            response["multiValueHeaders"] = {"Set-Cookie": cookies}
        return response

    @staticmethod
    def _safe_log(**fields: object) -> None:
        safe = {key: value for key, value in fields.items() if key in _SAFE_LOG_FIELDS}
        _LOG.info("economy_api_event %s", json.dumps(safe, separators=(",", ":")))
