from __future__ import annotations

import json
import hashlib
import base64
import unittest
from datetime import UTC, datetime, timedelta
from http.cookies import SimpleCookie
from unittest.mock import patch

from economy_api.app import ApiApplication, AppConfig
from economy_api.memory_repository import MemoryRepository
from economy_api.models import CrawlerStatus, User
from economy_api.security import CSRF_COOKIE, SESSION_COOKIE, hash_password
from economy_api.store import MemoryEconomyStore

ORIGIN = "https://economy.example.test"
PASSWORD = "correct horse battery staple"
NOW = datetime(2026, 10, 5, 12, 0, tzinfo=UTC)


def desktop_export(session: dict[str, object]) -> dict[str, object]:
    unsigned = {"schema": "a2.farm.session-export.v1", "session": session}
    encoded = json.dumps(
        unsigned, ensure_ascii=False, separators=(",", ":")
    ).encode("utf-8")
    return {
        "schema": "a2.farm.session-export.v1",
        "payload_id": hashlib.sha256(encoded).hexdigest(),
        "session": session,
    }


def event(
    method: str,
    path: str,
    *,
    body: object | None = None,
    cookie: str | None = None,
    csrf: str | None = None,
    origin: str | None = ORIGIN,
    headers: dict[str, str] | None = None,
    ip: str = "203.0.113.7",
) -> dict[str, object]:
    request_headers = dict(headers or {})
    if origin is not None:
        request_headers["Origin"] = origin
    if cookie:
        request_headers["Cookie"] = cookie
    if csrf:
        request_headers["X-CSRF-Token"] = csrf
    return {
        "httpMethod": method,
        "path": path,
        "headers": request_headers,
        "body": "" if body is None else json.dumps(body),
        "requestContext": {
            "requestId": f"test-{method}-{path}",
            "identity": {"sourceIp": ip},
        },
    }


def body(response: dict[str, object]) -> dict[str, object]:
    value = response.get("body") or "{}"
    return json.loads(str(value))


class ApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.repo = MemoryRepository()
        password_hash = hash_password(PASSWORD, salt=b"0123456789abcdef")
        for name, role in (
            ("rootadmin", "admin"),
            ("approved", "approved"),
            ("second", "approved"),
            ("waiting", "registered"),
        ):
            self.repo.create_user(
                User(
                    username=name,
                    user_id=f"id-{name}",
                    password_hash=password_hash,
                    role=role,  # type: ignore[arg-type]
                    created_at=NOW,
                    updated_at=NOW,
                )
            )
        self.store = MemoryEconomyStore(
            {
                ("market", "default"): {"revision": "r1", "items": [{"item_id": "42"}]},
                ("movers", "default"): {"revision": "r1", "up": [{"item_id": "42"}]},
            }
        )
        self.app = ApiApplication(
            self.repo,
            self.store,
            b"test-session-secret-must-be-at-least-32-bytes",
            AppConfig(allowed_origins=(ORIGIN,)),
            clock=lambda: NOW,
        )

    def login(self, username: str) -> tuple[str, str]:
        response = self.app.handle(
            event(
                "POST",
                "/api/v1/auth/login",
                body={"username": username, "password": PASSWORD},
                ip=f"198.51.100.{len(self.repo.sessions) + 1}",
            )
        )
        self.assertEqual(response["statusCode"], 200, response)
        cookie_values: dict[str, str] = {}
        for header in response["multiValueHeaders"]["Set-Cookie"]:  # type: ignore[index]
            parsed = SimpleCookie()
            parsed.load(header)
            for key, morsel in parsed.items():
                cookie_values[key] = morsel.value
        cookie = "; ".join(f"{key}={value}" for key, value in cookie_values.items())
        return cookie, cookie_values[CSRF_COOKIE]

    def test_registration_is_registered_and_duplicate_response_is_indistinguishable(self) -> None:
        payload = {"username": "new_user", "password": PASSWORD}
        first = self.app.handle(event("POST", "/api/v1/auth/register", body=payload))
        second = self.app.handle(
            event("POST", "/api/v1/auth/register", body=payload, ip="203.0.113.8")
        )
        self.assertEqual(first["statusCode"], 202)
        self.assertEqual(body(first), body(second))
        self.assertEqual(self.repo.get_user("new_user").role, "registered")  # type: ignore[union-attr]

    def test_registration_can_be_disabled_before_paid_work(self) -> None:
        app = ApiApplication(
            self.repo,
            self.store,
            self.app.secret,
            AppConfig(registration_enabled=False),
            clock=lambda: NOW,
        )
        with patch("economy_api.app.hash_password") as password_hash:
            response = app.handle(
                event(
                    "POST",
                    "/api/v1/auth/register",
                    body={"username": "blocked-user", "password": PASSWORD},
                )
            )
        self.assertEqual(response["statusCode"], 404)
        self.assertEqual(body(response), {"error": "not_found"})
        password_hash.assert_not_called()
        self.assertIsNone(self.repo.get_user("blocked-user"))

    def test_write_fence_blocks_all_mutations_but_keeps_health_readable(self) -> None:
        app = ApiApplication(
            self.repo,
            self.store,
            self.app.secret,
            AppConfig(write_fenced=True),
            clock=lambda: NOW,
        )
        blocked = app.handle(
            event(
                "POST",
                "/api/v1/auth/login",
                body={"username": "rootadmin", "password": PASSWORD},
            )
        )
        health = app.handle(event("GET", "/api/v1/health"))
        self.assertEqual(503, blocked["statusCode"])
        self.assertEqual({"error": "write_fenced"}, body(blocked))
        self.assertEqual(200, health["statusCode"])

    def test_registration_reports_safe_dependency_failures(self) -> None:
        with patch("economy_api.app.hash_password", side_effect=MemoryError("private")):
            password_failure = self.app.handle(
                event(
                    "POST",
                    "/api/v1/auth/register",
                    body={"username": "hash-failure", "password": PASSWORD},
                )
            )
        self.assertEqual(password_failure["statusCode"], 503)
        self.assertEqual(body(password_failure), {"error": "password_hash_unavailable"})

        with patch.object(
            self.repo,
            "create_user",
            side_effect=RuntimeError("private database details"),
        ):
            database_failure = self.app.handle(
                event(
                    "POST",
                    "/api/v1/auth/register",
                    body={"username": "database-failure", "password": PASSWORD},
                    ip="203.0.113.88",
                )
            )
        self.assertEqual(database_failure["statusCode"], 503)
        self.assertEqual(body(database_failure), {"error": "database_unavailable"})

    def test_failure_logs_never_include_exception_text_or_traceback(self) -> None:
        private_marker = "PRIVATE-DEPENDENCY-MARKER-9f3a"
        with self.assertLogs("aion2_economy_api", level="INFO") as captured:
            with patch(
                "economy_api.app.hash_password",
                side_effect=MemoryError(private_marker),
            ):
                dependency_failure = self.app.handle(
                    event(
                        "POST",
                        "/api/v1/auth/register",
                        body={"username": "safe-logging", "password": PASSWORD},
                    )
                )
        dependency_logs = "\n".join(captured.output)
        self.assertEqual(dependency_failure["statusCode"], 503)
        self.assertNotIn(private_marker, dependency_logs)
        self.assertNotIn("Traceback", dependency_logs)
        self.assertIn("password_hash_unavailable", dependency_logs)

        with self.assertLogs("aion2_economy_api", level="INFO") as captured:
            with patch.object(
                self.app,
                "_dispatch",
                side_effect=RuntimeError(private_marker),
            ):
                internal_failure = self.app.handle(event("GET", "/api/v1/health"))
        internal_logs = "\n".join(captured.output)
        self.assertEqual(internal_failure["statusCode"], 500)
        self.assertNotIn(private_marker, internal_logs)
        self.assertNotIn("Traceback", internal_logs)
        self.assertIn("internal_error", internal_logs)

    def test_login_error_is_generic_and_cookie_is_hardened(self) -> None:
        missing = self.app.handle(
            event(
                "POST",
                "/api/v1/auth/login",
                body={"username": "nobody", "password": PASSWORD},
            )
        )
        wrong = self.app.handle(
            event(
                "POST",
                "/api/v1/auth/login",
                body={"username": "approved", "password": "wrong password here"},
                ip="203.0.113.9",
            )
        )
        self.assertEqual(body(missing), {"error": "invalid_credentials"})
        self.assertEqual(body(wrong), {"error": "invalid_credentials"})
        response = self.app.handle(
            event(
                "POST",
                "/api/v1/auth/login",
                body={"username": "approved", "password": PASSWORD},
                ip="203.0.113.10",
            )
        )
        cookie_headers = response["multiValueHeaders"]["Set-Cookie"]  # type: ignore[index]
        session_header = next(value for value in cookie_headers if SESSION_COOKIE in value)
        self.assertIn("Secure", session_header)
        self.assertIn("HttpOnly", session_header)
        self.assertIn("SameSite=Strict", session_header)

    def test_login_rehashes_supported_legacy_password_profile(self) -> None:
        salt = b"0123456789abcdef"
        derived = hashlib.scrypt(
            PASSWORD.encode("utf-8"), salt=salt, n=1 << 14, r=8, p=1, dklen=32
        )
        encode = lambda value: base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")
        legacy = f"scrypt$16384$8$1${encode(salt)}${encode(derived)}"
        self.repo.set_user_password_hash("approved", legacy, NOW)

        response = self.app.handle(
            event(
                "POST",
                "/api/v1/auth/login",
                body={"username": "approved", "password": PASSWORD},
                ip="203.0.113.19",
            )
        )

        self.assertEqual(response["statusCode"], 200)
        upgraded = self.repo.get_user("approved")
        self.assertIsNotNone(upgraded)
        self.assertTrue(upgraded.password_hash.startswith("scrypt$131072$8$1$"))  # type: ignore[union-attr]

    def test_role_matrix_and_revoke_take_effect_on_existing_session(self) -> None:
        waiting_cookie, _ = self.login("waiting")
        denied = self.app.handle(
            event("GET", "/api/v1/market/snapshot", cookie=waiting_cookie)
        )
        self.assertEqual(denied["statusCode"], 403)

        admin_cookie, admin_csrf = self.login("rootadmin")
        approved = self.app.handle(
            event(
                "POST",
                "/api/v1/admin/users/waiting/role",
                body={"role": "approved"},
                cookie=admin_cookie,
                csrf=admin_csrf,
            )
        )
        self.assertEqual(approved["statusCode"], 200)
        market = self.app.handle(
            event("GET", "/api/v1/market/snapshot", cookie=waiting_cookie)
        )
        self.assertEqual(market["statusCode"], 200)
        self.assertEqual(body(market)["revision"], "r1")

        revoked = self.app.handle(
            event(
                "POST",
                "/api/v1/admin/users/waiting/role",
                body={"role": "registered"},
                cookie=admin_cookie,
                csrf=admin_csrf,
                ip="203.0.113.33",
            )
        )
        self.assertEqual(revoked["statusCode"], 200)
        denied_again = self.app.handle(
            event("GET", "/api/v1/market/snapshot", cookie=waiting_cookie)
        )
        self.assertEqual(denied_again["statusCode"], 403)

    def test_market_and_economy_are_approved_only(self) -> None:
        guest = self.app.handle(event("GET", "/api/v1/market/snapshot", origin=None))
        self.assertEqual(guest["statusCode"], 401)
        cookie, _ = self.login("approved")
        market = self.app.handle(event("GET", "/api/v1/market/snapshot", cookie=cookie))
        movers = self.app.handle(event("GET", "/api/v1/economy/movers", cookie=cookie))
        missing = self.app.handle(event("GET", "/api/v1/economy/craft", cookie=cookie))
        self.assertEqual(market["statusCode"], 200)
        self.assertEqual(movers["statusCode"], 200)
        self.assertEqual(missing["statusCode"], 404)

    def test_same_origin_does_not_require_gateway_domain_bootstrap(self) -> None:
        app = ApiApplication(
            self.repo,
            self.store,
            b"test-session-secret-must-be-at-least-32-bytes",
            AppConfig(),
            clock=lambda: NOW,
        )
        response = app.handle(
            event(
                "POST",
                "/api/v1/auth/login",
                body={"username": "approved", "password": PASSWORD},
                headers={"Host": "economy.example.test"},
            )
        )
        self.assertEqual(response["statusCode"], 200)
        rejected = app.handle(
            event(
                "POST",
                "/api/v1/auth/login",
                body={"username": "approved", "password": PASSWORD},
                headers={"Host": "economy.example.test"},
                origin="https://different.example.test",
                ip="203.0.113.80",
            )
        )
        self.assertEqual(rejected["statusCode"], 403)

    def test_state_change_requires_same_origin_csrf(self) -> None:
        cookie, csrf = self.login("approved")
        payload = {
            "client_session_id": "client-1",
            "duration_seconds": 60,
            "total_kina": 1200,
            "loot": [],
        }
        no_csrf = self.app.handle(
            event("POST", "/api/v1/farm/sessions", body=payload, cookie=cookie)
        )
        foreign_origin = self.app.handle(
            event(
                "POST",
                "/api/v1/farm/sessions",
                body=payload,
                cookie=cookie,
                csrf=csrf,
                origin="https://evil.invalid",
            )
        )
        self.assertEqual(no_csrf["statusCode"], 403)
        self.assertEqual(foreign_origin["statusCode"], 403)

    def test_farm_crud_is_owner_scoped_and_admin_can_list_global(self) -> None:
        owner_cookie, owner_csrf = self.login("approved")
        payload = {
            "client_session_id": "client-1",
            "duration_seconds": 120,
            "total_kina": 2400,
            "server": "EU-1",
            "loot": [
                {"item_id": "516150001", "quantity": 2, "estimated_unit_price": 400000}
            ],
        }
        created = self.app.handle(
            event(
                "POST",
                "/api/v1/farm/sessions",
                body=payload,
                cookie=owner_cookie,
                csrf=owner_csrf,
            )
        )
        self.assertEqual(created["statusCode"], 201, created)
        session = body(created)["session"]
        self.assertEqual(session["kina_per_second"], 20.0)

        second_cookie, _ = self.login("second")
        invisible = self.app.handle(
            event("GET", f"/api/v1/farm/sessions/{session['id']}", cookie=second_cookie)
        )
        self.assertEqual(invisible["statusCode"], 404)

        admin_cookie, admin_csrf = self.login("rootadmin")
        global_rows = self.app.handle(
            event("GET", "/api/v1/admin/farm-sessions", cookie=admin_cookie)
        )
        self.assertEqual(len(body(global_rows)["sessions"]), 1)

        deleted = self.app.handle(
            event(
                "DELETE",
                f"/api/v1/farm/sessions/{session['id']}",
                cookie=owner_cookie,
                csrf=owner_csrf,
            )
        )
        self.assertEqual(deleted["statusCode"], 204)

    def test_farm_payload_is_bounded_and_client_kps_is_ignored(self) -> None:
        cookie, csrf = self.login("approved")
        oversized = {
            "client_session_id": "too-many",
            "duration_seconds": 1,
            "total_kina": 1,
            "kina_per_second": 999999,
            "loot": [
                {"item_id": str(i), "quantity": 1, "estimated_unit_price": 1}
                for i in range(501)
            ],
        }
        response = self.app.handle(
            event(
                "POST",
                "/api/v1/farm/sessions",
                body=oversized,
                cookie=cookie,
                csrf=csrf,
            )
        )
        self.assertEqual(response["statusCode"], 400)

    def test_farm_session_limit_blocks_only_new_rows(self) -> None:
        cookie, csrf = self.login("approved")
        first_payload = {
            "client_session_id": "bounded-1",
            "duration_seconds": 1,
            "total_kina": 1,
            "loot": [],
        }
        second_payload = {**first_payload, "client_session_id": "bounded-2"}
        with patch("economy_api.app.MAX_FARM_SESSIONS_PER_USER", 1):
            first = self.app.handle(event(
                "POST", "/api/v1/farm/sessions", body=first_payload,
                cookie=cookie, csrf=csrf,
            ))
            retry = self.app.handle(event(
                "POST", "/api/v1/farm/sessions", body=first_payload,
                cookie=cookie, csrf=csrf,
            ))
            blocked = self.app.handle(event(
                "POST", "/api/v1/farm/sessions", body=second_payload,
                cookie=cookie, csrf=csrf,
            ))
        self.assertEqual(first["statusCode"], 201, first)
        self.assertEqual(retry["statusCode"], 200, retry)
        self.assertEqual(blocked["statusCode"], 409, blocked)
        self.assertEqual(body(blocked)["error"], "farm_session_limit_reached")
        self.assertEqual(self.repo.count_farm_sessions("approved"), 1)

    def test_desktop_farm_export_v1_maps_values_and_is_idempotent(self) -> None:
        cookie, csrf = self.login("approved")
        payload = desktop_export({
                "session_id": "desktop-session-001",
                "activity": {"kind": "dungeon", "label": "Secret Library"},
                "started_at_ms": 1_790_000_000_000,
                "stopped_at_ms": 1_790_000_060_000,
                "observed_direct_kina": 1200,
                "estimated_loot_value": 800000,
                "estimated_total_value": 801200,
                "event_count": 42,
                "loot": [
                    {
                        "item_id": 516150001,
                        "quantity": 2,
                        "unit_price": 400000,
                        "market_server_id": 101,
                        "price_observed_at_ms": 1_790_000_030_000,
                        "price_source": "market-current",
                        "estimated_value": 800000,
                    }
                ],
        })
        payload_id = str(payload["payload_id"])
        first = self.app.handle(
            event(
                "POST",
                "/api/v1/farm/sessions",
                body=payload,
                cookie=cookie,
                csrf=csrf,
                headers={"Idempotency-Key": payload_id},
            )
        )
        second = self.app.handle(
            event(
                "POST",
                "/api/v1/farm/sessions",
                body=payload,
                cookie=cookie,
                csrf=csrf,
                ip="203.0.113.51",
                headers={"Idempotency-Key": payload_id},
            )
        )
        self.assertEqual(first["statusCode"], 201, first)
        first_row = body(first)["session"]
        second_row = body(second)["session"]
        self.assertEqual(first_row["id"], second_row["id"])
        self.assertEqual(first_row["observed_direct_kina"], 1200)
        self.assertEqual(first_row["estimated_loot_value"], 800000)
        self.assertEqual(first_row["estimated_total_value"], 801200)
        self.assertEqual(first_row["activity"], {"kind": "dungeon", "label": "Secret Library"})
        self.assertEqual(first_row["loot"][0]["market_server_id"], 101)
        self.assertEqual(first_row["loot"][0]["price_source"], "market-current")
        self.assertEqual(first_row["loot"][0]["price_observed_at_ms"], 1_790_000_030_000)
        self.assertEqual(first_row["duration_seconds"], 60.0)
        self.assertEqual(len(self.repo.farm_sessions), 1)

        polluted = dict(payload)
        polluted["jwt"] = "must-be-rejected"
        rejected = self.app.handle(
            event(
                "POST",
                "/api/v1/farm/sessions",
                body=polluted,
                cookie=cookie,
                csrf=csrf,
                headers={"Idempotency-Key": payload_id},
            )
        )
        self.assertEqual(rejected["statusCode"], 400)

        tampered = json.loads(json.dumps(payload))
        tampered["session"]["observed_direct_kina"] = 1201
        tampered_hash = self.app.handle(
            event(
                "POST", "/api/v1/farm/sessions", body=tampered,
                cookie=cookie, csrf=csrf,
                headers={"Idempotency-Key": payload_id},
            )
        )
        self.assertEqual(tampered_hash["statusCode"], 400)
        self.assertEqual(body(tampered_hash)["error"], "farm_payload_hash_mismatch")

    def test_desktop_farm_export_accepts_unpriced_loot_without_inventing_value(
        self,
    ) -> None:
        cookie, csrf = self.login("approved")
        payload = desktop_export({
                "session_id": "desktop-session-unpriced",
                "activity": None,
                "started_at_ms": 1_790_000_000_000,
                "stopped_at_ms": 1_790_000_060_000,
                "observed_direct_kina": 1200,
                "estimated_loot_value": 0,
                "estimated_total_value": 1200,
                "event_count": 2,
                "loot": [
                    {
                        "item_id": 516150001,
                        "quantity": 3,
                        "unit_price": None,
                        "market_server_id": None,
                        "price_observed_at_ms": None,
                        "price_source": None,
                        "estimated_value": None,
                    }
                ],
        })
        payload_id = str(payload["payload_id"])
        created = self.app.handle(
            event(
                "POST",
                "/api/v1/farm/sessions",
                body=payload,
                cookie=cookie,
                csrf=csrf,
                headers={"Idempotency-Key": payload_id},
            )
        )
        self.assertEqual(created["statusCode"], 201, created)
        session = body(created)["session"]
        self.assertEqual(session["estimated_loot_value"], 0)
        self.assertIsNone(session["loot"][0]["estimated_unit_price"])
        self.assertIsNone(session["loot"][0]["estimated_value"])

    def test_desktop_farm_export_rejects_unbounded_activity_metadata(self) -> None:
        cookie, csrf = self.login("approved")
        payload = desktop_export({
                "session_id": "desktop-session-invalid-activity",
                "activity": {"kind": "spot", "label": "x" * 81},
                "started_at_ms": 1_790_000_000_000,
                "stopped_at_ms": 1_790_000_001_000,
                "observed_direct_kina": 1,
                "estimated_loot_value": 0,
                "estimated_total_value": 1,
                "event_count": 1,
                "loot": [],
        })
        rejected = self.app.handle(
            event(
                "POST",
                "/api/v1/farm/sessions",
                body=payload,
                cookie=cookie,
                csrf=csrf,
                headers={"Idempotency-Key": str(payload["payload_id"])},
            )
        )
        self.assertEqual(rejected["statusCode"], 400, rejected)
        self.assertEqual(body(rejected)["error"], "invalid_farm_session")

    def test_desktop_farm_export_accepts_signed_wallet_delta(self) -> None:
        cookie, csrf = self.login("approved")
        payload = desktop_export({
                "session_id": "expense-session",
                "activity": {"kind": "spot", "label": None},
                "started_at_ms": 1_790_000_000_000,
                "stopped_at_ms": 1_790_000_001_000,
                "observed_direct_kina": -500,
                "estimated_loot_value": 120,
                "estimated_total_value": -380,
                "event_count": 2,
                "loot": [
                    {
                        "item_id": 42,
                        "quantity": 2,
                        "unit_price": 60,
                        "market_server_id": 101,
                        "price_observed_at_ms": 1_790_000_000_500,
                        "price_source": "market-summary",
                        "estimated_value": 120,
                    }
                ],
        })
        created = self.app.handle(
            event(
                "POST",
                "/api/v1/farm/sessions",
                body=payload,
                cookie=cookie,
                csrf=csrf,
                headers={"Idempotency-Key": str(payload["payload_id"])},
            )
        )
        self.assertEqual(created["statusCode"], 201, created)
        session = body(created)["session"]
        self.assertEqual(session["observed_direct_kina"], -500)
        self.assertEqual(session["estimated_total_value"], -380)

    def test_scoped_desktop_token_sync_receipt_idempotency_and_revocation(self) -> None:
        cookie, csrf = self.login("approved")
        issued = self.app.handle(
            event(
                "POST",
                "/api/v1/farm/sync-tokens",
                body={"label": "Main desktop", "expires_in_seconds": 600},
                cookie=cookie,
                csrf=csrf,
            )
        )
        self.assertEqual(issued["statusCode"], 201, issued)
        issued_body = body(issued)
        token_value = str(issued_body["token"])
        token_meta = issued_body["token_meta"]
        token_id = str(token_meta["id"])
        stored = self.repo.get_desktop_sync_token(token_id)
        self.assertIsNotNone(stored)
        self.assertNotEqual(stored.token_digest, token_value)  # type: ignore[union-attr]
        self.assertNotIn(token_value, repr(self.repo.desktop_sync_tokens))
        self.assertEqual(issued["headers"]["Cache-Control"], "no-store")

        listed = self.app.handle(
            event("GET", "/api/v1/farm/sync-tokens", cookie=cookie)
        )
        self.assertEqual(listed["statusCode"], 200)
        self.assertNotIn(token_value, json.dumps(body(listed)))
        self.assertEqual(body(listed)["tokens"][0]["scope"], "farm:write")

        payload = desktop_export({
                "session_id": "desktop-direct-sync-1",
                "activity": None,
                "started_at_ms": 1_790_000_000_000,
                "stopped_at_ms": 1_790_000_001_000,
                "observed_direct_kina": 7,
                "estimated_loot_value": 0,
                "estimated_total_value": 7,
                "event_count": 1,
                "loot": [],
        })
        payload_id = str(payload["payload_id"])
        desktop_headers = {
            "Authorization": f"Bearer {token_value}",
            "Idempotency-Key": payload_id,
        }
        with self.assertLogs("aion2_economy_api", level="INFO") as captured:
            first = self.app.handle(
                event(
                    "POST",
                    "/api/v1/desktop/farm/sessions",
                    body=payload,
                    headers=desktop_headers,
                )
            )
            second = self.app.handle(
                event(
                    "POST",
                    "/api/v1/desktop/farm/sessions",
                    body=payload,
                    headers=desktop_headers,
                    ip="203.0.113.99",
                )
            )
        self.assertNotIn(token_value, "\n".join(captured.output))
        expected_receipt = body(first)
        self.assertEqual(first["statusCode"], 200, first)
        self.assertEqual(second["statusCode"], 200, second)
        self.assertEqual(expected_receipt, body(second))
        self.assertEqual(
            expected_receipt,
            {
                "schema": "a2.farm.sync-receipt.v1",
                "payload_id": payload_id,
                "accepted": True,
                "receipt_id": expected_receipt["receipt_id"],
            },
        )
        self.assertRegex(str(expected_receipt["receipt_id"]), r"^[A-Za-z0-9._:-]+$")
        self.assertEqual(len(self.repo.farm_sessions), 1)

        market_with_bearer_only = self.app.handle(
            event(
                "GET",
                "/api/v1/market/snapshot",
                headers={"Authorization": f"Bearer {token_value}"},
            )
        )
        self.assertEqual(market_with_bearer_only["statusCode"], 401)

        revoked = self.app.handle(
            event(
                "DELETE",
                f"/api/v1/farm/sync-tokens/{token_id}",
                cookie=cookie,
                csrf=csrf,
            )
        )
        self.assertEqual(revoked["statusCode"], 204)
        rejected = self.app.handle(
            event(
                "POST",
                "/api/v1/desktop/farm/sessions",
                body=payload,
                headers=desktop_headers,
            )
        )
        self.assertEqual(rejected["statusCode"], 401)
        self.assertEqual(body(rejected), {"error": "desktop_token_required"})

    def test_desktop_token_requires_approved_browser_session_and_csrf(self) -> None:
        waiting_cookie, waiting_csrf = self.login("waiting")
        waiting = self.app.handle(
            event(
                "POST",
                "/api/v1/farm/sync-tokens",
                body={},
                cookie=waiting_cookie,
                csrf=waiting_csrf,
            )
        )
        self.assertEqual(waiting["statusCode"], 403)

        cookie, _ = self.login("approved")
        without_csrf = self.app.handle(
            event(
                "POST",
                "/api/v1/farm/sync-tokens",
                body={},
                cookie=cookie,
            )
        )
        self.assertEqual(without_csrf["statusCode"], 403)

    def test_admin_cannot_demote_self_or_promote_another_admin(self) -> None:
        cookie, csrf = self.login("rootadmin")
        response = self.app.handle(
            event(
                "POST",
                "/api/v1/admin/users/rootadmin/role",
                body={"role": "registered"},
                cookie=cookie,
                csrf=csrf,
            )
        )
        self.assertEqual(response["statusCode"], 409)

    def test_api_rate_limit_is_enforced_by_repository(self) -> None:
        for index in range(5):
            response = self.app.handle(
                event(
                    "POST",
                    "/api/v1/auth/register",
                    body={"username": f"rate{index}", "password": PASSWORD},
                    ip="192.0.2.50",
                )
            )
            self.assertEqual(response["statusCode"], 202)
        limited = self.app.handle(
            event(
                "POST",
                "/api/v1/auth/register",
                body={"username": "rate-last", "password": PASSWORD},
                ip="192.0.2.50",
            )
        )
        self.assertEqual(limited["statusCode"], 429)

    def test_crawler_admin_status_and_idempotent_action_queue(self) -> None:
        approved_cookie, _ = self.login("approved")
        denied = self.app.handle(
            event("GET", "/api/v1/admin/crawler/status", cookie=approved_cookie)
        )
        self.assertEqual(denied["statusCode"], 403)

        admin_cookie, admin_csrf = self.login("rootadmin")
        status = self.app.handle(
            event("GET", "/api/v1/admin/crawler/status", cookie=admin_cookie)
        )
        self.assertEqual(body(status)["status"], "not_connected")
        self.assertEqual(body(status)["revision"], 0)
        disconnected = self.app.handle(
            event(
                "POST",
                "/api/v1/admin/crawler/actions",
                body={
                    "idempotencyKey": "ui-action-disconnected",
                    "action": "retry",
                    "expectedRevision": 0,
                },
                cookie=admin_cookie,
                csrf=admin_csrf,
            )
        )
        self.assertEqual(disconnected["statusCode"], 409)

        self.repo.crawler_status = CrawlerStatus(
            run_id="run-7",
            status="running",
            phase="items",
            revision=7,
            updated_at=NOW,
            started_at=NOW,
            elapsed_seconds=12,
            total_items=100,
            processed_items=25,
            successful_requests=24,
            failed_requests=1,
            retried_requests=2,
            queue_depth=1,
            checkpoint={
                "cursor": "item:25",
                "processedItems": 25,
                "savedAt": "2026-10-05T12:00:00Z",
                "revision": 7,
            },
            errors=(),
        )
        payload = {
            "idempotencyKey": "ui-action-20261005-1",
            "action": "pause",
            "expectedRevision": 7,
        }
        first = self.app.handle(
            event(
                "POST",
                "/api/v1/admin/crawler/actions",
                body=payload,
                cookie=admin_cookie,
                csrf=admin_csrf,
            )
        )
        second = self.app.handle(
            event(
                "POST",
                "/api/v1/admin/crawler/actions",
                body=payload,
                cookie=admin_cookie,
                csrf=admin_csrf,
                ip="203.0.113.99",
            )
        )
        self.assertEqual(first["statusCode"], 202)
        self.assertEqual(body(first), body(second))
        self.assertEqual(set(body(first)), {"status"})
        self.assertEqual(body(first)["status"]["status"], "running")
        listed = self.app.handle(
            event("GET", "/api/v1/admin/crawler/actions", cookie=admin_cookie)
        )
        self.assertEqual(len(body(listed)["actions"]), 1)
        self.assertEqual(body(listed)["actions"][0]["expectedRevision"], 7)

        stale = self.app.handle(
            event(
                "POST",
                "/api/v1/admin/crawler/actions",
                body={
                    "idempotencyKey": "ui-action-stale",
                    "action": "pause",
                    "expectedRevision": 6,
                },
                cookie=admin_cookie,
                csrf=admin_csrf,
            )
        )
        invalid_transition = self.app.handle(
            event(
                "POST",
                "/api/v1/admin/crawler/actions",
                body={
                    "idempotencyKey": "ui-action-invalid",
                    "action": "resume",
                    "expectedRevision": 7,
                },
                cookie=admin_cookie,
                csrf=admin_csrf,
            )
        )
        self.assertEqual(stale["statusCode"], 409)
        self.assertEqual(invalid_transition["statusCode"], 409)
        self.assertEqual(len(self.repo.crawler_actions), 1)

        self.repo.crawler_status = CrawlerStatus(
            run_id="run-7",
            status="running",
            phase="items",
            revision=8,
            updated_at=NOW,
            started_at=NOW,
            elapsed_seconds=12,
            total_items=100,
            processed_items=25,
            successful_requests=24,
            failed_requests=1,
            retried_requests=2,
            queue_depth=1,
            checkpoint={
                "cursor": "item:25",
                "processedItems": 25,
                "savedAt": "2026-10-05T12:00:00Z",
                "revision": 8,
            },
            errors=(
                {
                    "errorId": "err-1",
                    "at": "2026-10-05T12:00:00Z",
                    "code": "timeout",
                    "errorClass": "TimeoutError",
                    "message": "request timed out",
                    "retryable": True,
                    "itemId": "516150001",
                    "attempt": 2,
                },
            ),
        )
        populated = self.app.handle(
            event("GET", "/api/v1/admin/crawler/status", cookie=admin_cookie)
        )
        status_payload = body(populated)
        self.assertEqual(
            set(status_payload),
            {
                "configured",
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
            },
        )
        self.assertEqual(status_payload["processedItems"], 25)
        self.assertEqual(status_payload["checkpoint"]["cursor"], "item:25")
        self.assertTrue(status_payload["errors"][0]["retryable"])

    def test_standalone_object_status_fallback_is_read_only_and_revision_exact(self) -> None:
        standalone = {
            "configured": True,
            "source": "standalone_object",
            "controlAvailable": False,
            "economyRevision": "b25650019-r1",
            "runId": None,
            "status": "completed",
            "phase": "complete",
            "revision": 0,
            "updatedAt": "2026-10-05T11:59:00Z",
            "startedAt": None,
            "elapsedSeconds": 0,
            "totalItems": 100,
            "processedItems": 100,
            "successfulRequests": 100,
            "failedRequests": 0,
            "retriedRequests": 0,
            "queueDepth": 0,
            "checkpoint": None,
            "errors": [],
        }
        self.store.read_crawler_status = lambda _now: standalone  # type: ignore[attr-defined]
        admin_cookie, admin_csrf = self.login("rootadmin")

        response = self.app.handle(
            event("GET", "/api/v1/admin/crawler/status", cookie=admin_cookie)
        )

        self.assertEqual(response["statusCode"], 200)
        self.assertEqual(body(response), standalone)
        action = self.app.handle(
            event(
                "POST",
                "/api/v1/admin/crawler/actions",
                body={
                    "idempotencyKey": "standalone-action",
                    "action": "retry",
                    "expectedRevision": 0,
                },
                cookie=admin_cookie,
                csrf=admin_csrf,
            )
        )
        self.assertEqual(action["statusCode"], 409)

    def test_exact_standalone_status_wins_over_fresh_legacy_control_plane(self) -> None:
        self.repo.crawler_status = CrawlerStatus(
            run_id="run-fresh",
            status="running",
            phase="scan",
            revision=9,
            updated_at=NOW - timedelta(seconds=30),
            started_at=NOW - timedelta(seconds=60),
            elapsed_seconds=60,
            total_items=100,
            processed_items=20,
            successful_requests=20,
            failed_requests=0,
            retried_requests=0,
            queue_depth=80,
            checkpoint=None,
            errors=(),
        )
        standalone_status = {
            "configured": True,
            "source": "standalone_object",
            "controlAvailable": False,
            "economyRevision": "b25650019-r-current",
            "runId": "market-0123456789abcdef0123456789abcdef",
            "status": "completed",
            "phase": "complete",
        }

        def standalone(_now: datetime) -> dict[str, object]:
            return standalone_status

        self.store.read_crawler_status = standalone  # type: ignore[attr-defined]
        admin_cookie, _ = self.login("rootadmin")

        response = self.app.handle(
            event("GET", "/api/v1/admin/crawler/status", cookie=admin_cookie)
        )

        self.assertEqual(body(response), standalone_status)

    def test_fresh_control_plane_is_fallback_when_standalone_read_fails(self) -> None:
        self.repo.crawler_status = CrawlerStatus(
            run_id="run-fresh",
            status="running",
            phase="scan",
            revision=9,
            updated_at=NOW - timedelta(seconds=30),
            started_at=NOW - timedelta(seconds=60),
            elapsed_seconds=60,
            total_items=100,
            processed_items=20,
            successful_requests=20,
            failed_requests=0,
            retried_requests=0,
            queue_depth=80,
            checkpoint=None,
            errors=(),
        )

        def standalone(_now: datetime) -> dict[str, object]:
            raise RuntimeError("object-store-unavailable")

        self.store.read_crawler_status = standalone  # type: ignore[attr-defined]
        admin_cookie, _ = self.login("rootadmin")

        response = self.app.handle(
            event("GET", "/api/v1/admin/crawler/status", cookie=admin_cookie)
        )

        self.assertEqual(body(response)["runId"], "run-fresh")

    def test_stale_control_plane_status_yields_to_standalone_object(self) -> None:
        self.repo.crawler_status = CrawlerStatus(
            run_id="run-stale",
            status="running",
            phase="scan",
            revision=8,
            updated_at=NOW - timedelta(seconds=421),
            started_at=NOW - timedelta(seconds=500),
            elapsed_seconds=500,
            total_items=100,
            processed_items=20,
            successful_requests=20,
            failed_requests=0,
            retried_requests=0,
            queue_depth=80,
            checkpoint=None,
            errors=(),
        )
        self.store.read_crawler_status = lambda _now: {  # type: ignore[attr-defined]
            "configured": True,
            "source": "standalone_object",
            "controlAvailable": False,
            "economyRevision": "b25650019-r2",
            "status": "completed",
        }
        admin_cookie, admin_csrf = self.login("rootadmin")

        response = self.app.handle(
            event("GET", "/api/v1/admin/crawler/status", cookie=admin_cookie)
        )

        self.assertEqual(body(response)["source"], "standalone_object")
        self.assertEqual(body(response)["economyRevision"], "b25650019-r2")
        action = self.app.handle(
            event(
                "POST",
                "/api/v1/admin/crawler/actions",
                body={
                    "idempotencyKey": "stale-direct-action",
                    "action": "pause",
                    "expectedRevision": 8,
                },
                cookie=admin_cookie,
                csrf=admin_csrf,
            )
        )
        self.assertEqual(action["statusCode"], 409)
        self.assertEqual(body(action), {"error": "crawler_not_connected"})

    def test_stale_control_plane_without_standalone_is_not_connected(self) -> None:
        self.repo.crawler_status = CrawlerStatus(
            run_id="run-stale",
            status="running",
            phase="scan",
            revision=8,
            updated_at=NOW - timedelta(seconds=421),
            started_at=NOW - timedelta(seconds=500),
            elapsed_seconds=500,
            total_items=100,
            processed_items=20,
            successful_requests=20,
            failed_requests=0,
            retried_requests=0,
            queue_depth=80,
            checkpoint=None,
            errors=(),
        )
        self.store.read_crawler_status = lambda _now: None  # type: ignore[attr-defined]
        admin_cookie, _ = self.login("rootadmin")

        response = self.app.handle(
            event("GET", "/api/v1/admin/crawler/status", cookie=admin_cookie)
        )

        self.assertEqual(body(response)["status"], "not_connected")
        self.assertIsNone(body(response)["runId"])

    def test_malformed_standalone_status_fails_closed(self) -> None:
        def malformed(_now: datetime) -> dict[str, object]:
            raise ValueError("untrusted object details")

        self.store.read_crawler_status = malformed  # type: ignore[attr-defined]
        admin_cookie, _ = self.login("rootadmin")

        response = self.app.handle(
            event("GET", "/api/v1/admin/crawler/status", cookie=admin_cookie)
        )

        self.assertEqual(response["statusCode"], 503)
        self.assertEqual(body(response), {"error": "crawler_status_unavailable"})

    def test_crawler_control_token_heartbeat_claim_complete_and_revision_cas(self) -> None:
        admin_cookie, admin_csrf = self.login("rootadmin")
        issued = self.app.handle(
            event(
                "POST",
                "/api/v1/admin/crawler/tokens",
                body={"crawler_id": "primary", "expires_in_seconds": 600},
                cookie=admin_cookie,
                csrf=admin_csrf,
            )
        )
        self.assertEqual(issued["statusCode"], 201, issued)
        token_value = str(body(issued)["token"])
        token_id = str(body(issued)["token_meta"]["id"])
        authorization = {"Authorization": f"Bearer {token_value}"}
        initial_status = {
            "status": "running",
            "phase": "items",
            "updatedAt": "2099-10-05T12:00:00Z",
            "startedAt": "2026-10-05T11:59:00Z",
            "elapsedSeconds": 60,
            "totalItems": 100,
            "processedItems": 10,
            "successfulRequests": 10,
            "failedRequests": 0,
            "retriedRequests": 0,
            "queueDepth": 0,
            "checkpoint": {
                "cursor": "item:10",
                "processedItems": 10,
                "savedAt": "2026-10-05T12:00:00Z",
                "revision": 1,
            },
            "errors": [],
        }
        connected = self.app.handle(
            event(
                "POST",
                "/api/v1/crawler/connect",
                body={
                    "crawler_id": "primary",
                    "runId": "run-service-1",
                    "status": initial_status,
                },
                headers=authorization,
            )
        )
        self.assertEqual(connected["statusCode"], 200, connected)
        lease_id = str(body(connected)["leaseId"])
        self.assertEqual(body(connected)["status"]["revision"], 1)
        self.assertEqual(body(connected)["status"]["updatedAt"], "2026-10-05T12:00:00Z")
        self.assertEqual(self.repo.crawler_lease_updated_at, NOW)
        status = {
            **initial_status,
            "updatedAt": "2001-01-01T00:00:00Z",
            "runId": "run-service-1",
            "revision": 2,
            "processedItems": 11,
            "successfulRequests": 11,
            "errors": [{
                "errorId": "safe-error-1",
                "at": "2026-10-05T12:00:00Z",
                "code": "upstream_error",
                "errorClass": "upstream",
                "message": "authorization=private-value Bearer abc.def",
                "retryable": True,
            }],
        }
        heartbeat = self.app.handle(
            event(
                "POST",
                "/api/v1/crawler/heartbeat",
                body={
                    "crawler_id": "primary",
                    "runId": "run-service-1",
                    "leaseId": lease_id,
                    "expectedRevision": 1,
                    "status": status,
                },
                headers=authorization,
            )
        )
        self.assertEqual(heartbeat["statusCode"], 200, heartbeat)
        self.assertEqual(body(heartbeat)["status"]["revision"], 2)
        self.assertEqual(body(heartbeat)["status"]["updatedAt"], "2026-10-05T12:00:00Z")
        self.assertEqual(self.repo.crawler_lease_updated_at, NOW)
        rendered_errors = json.dumps(body(heartbeat)["status"]["errors"])
        self.assertNotIn("private-value", rendered_errors)
        self.assertNotIn("abc.def", rendered_errors)
        self.assertFalse(any(
            audit.kind == "crawler_heartbeat" for audit in self.repo.audit_events
        ))

        stale = self.app.handle(
            event(
                "POST",
                "/api/v1/crawler/heartbeat",
                body={
                    "crawler_id": "primary",
                    "runId": "run-service-1",
                    "leaseId": lease_id,
                    "expectedRevision": 1,
                    "status": status,
                },
                headers=authorization,
                ip="203.0.113.81",
            )
        )
        self.assertEqual(stale["statusCode"], 409)

        queued = self.app.handle(
            event(
                "POST",
                "/api/v1/admin/crawler/actions",
                body={
                    "idempotencyKey": "crawler-service-pause-1",
                    "action": "pause",
                    "expectedRevision": 2,
                },
                cookie=admin_cookie,
                csrf=admin_csrf,
            )
        )
        self.assertEqual(queued["statusCode"], 202, queued)
        claimed = self.app.handle(
            event(
                "POST",
                "/api/v1/crawler/actions/claim",
                body={
                    "crawler_id": "primary",
                    "runId": "run-service-1",
                    "leaseId": lease_id,
                    "currentRevision": 2,
                },
                headers=authorization,
            )
        )
        self.assertEqual(claimed["statusCode"], 200, claimed)
        action = body(claimed)["action"]
        self.assertEqual(action["status"], "claimed")
        self.assertEqual(action["claimedBy"], "primary")

        no_second_claim = self.app.handle(
            event(
                "POST",
                "/api/v1/crawler/actions/claim",
                body={
                    "crawler_id": "primary",
                    "runId": "run-service-1",
                    "leaseId": lease_id,
                    "currentRevision": 2,
                },
                headers=authorization,
                ip="203.0.113.82",
            )
        )
        self.assertEqual(no_second_claim["statusCode"], 200)
        self.assertIsNone(body(no_second_claim)["action"])

        completed = self.app.handle(
            event(
                "POST",
                f"/api/v1/crawler/actions/{action['id']}/complete",
                body={
                    "crawler_id": "primary",
                    "runId": "run-service-1",
                    "leaseId": lease_id,
                    "currentRevision": 2,
                    "outcome": "applied",
                },
                headers=authorization,
            )
        )
        self.assertEqual(completed["statusCode"], 200, completed)
        self.assertEqual(body(completed)["action"]["status"], "applied")

        same_run_without_lease = self.app.handle(
            event(
                "POST",
                "/api/v1/crawler/connect",
                body={
                    "crawler_id": "primary",
                    "runId": "run-service-1",
                    "status": initial_status,
                },
                headers=authorization,
                ip="203.0.113.86",
            )
        )
        self.assertEqual(same_run_without_lease["statusCode"], 409)

        reconnected = self.app.handle(
            event(
                "POST",
                "/api/v1/crawler/connect",
                body={
                    "crawler_id": "primary",
                    "runId": "run-service-2",
                    "status": initial_status,
                },
                headers=authorization,
                ip="203.0.113.84",
            )
        )
        self.assertEqual(reconnected["statusCode"], 409, reconnected)
        self.repo.crawler_lease_updated_at = NOW - timedelta(seconds=421)
        reconnected = self.app.handle(
            event(
                "POST",
                "/api/v1/crawler/connect",
                body={
                    "crawler_id": "primary",
                    "runId": "run-service-2",
                    "status": initial_status,
                },
                headers=authorization,
                ip="203.0.113.85",
            )
        )
        self.assertEqual(reconnected["statusCode"], 200, reconnected)
        self.assertEqual(body(reconnected)["status"]["revision"], 3)
        stale_lease = self.app.handle(
            event(
                "POST",
                "/api/v1/crawler/actions/claim",
                body={
                    "crawler_id": "primary",
                    "runId": "run-service-1",
                    "leaseId": lease_id,
                    "currentRevision": 2,
                },
                headers=authorization,
                ip="203.0.113.85",
            )
        )
        self.assertEqual(stale_lease["statusCode"], 409)

        wrong_scope = self.app.handle(
            event(
                "POST",
                "/api/v1/desktop/farm/sessions",
                body={},
                headers=authorization,
            )
        )
        self.assertEqual(wrong_scope["statusCode"], 401)

        revoked = self.app.handle(
            event(
                "DELETE",
                f"/api/v1/admin/crawler/tokens/{token_id}",
                cookie=admin_cookie,
                csrf=admin_csrf,
            )
        )
        self.assertEqual(revoked["statusCode"], 204)
        rejected = self.app.handle(
            event(
                "POST",
                "/api/v1/crawler/actions/claim",
                body={
                    "crawler_id": "primary",
                    "runId": "run-service-1",
                    "leaseId": lease_id,
                    "currentRevision": 2,
                },
                headers=authorization,
                ip="203.0.113.83",
            )
        )
        self.assertEqual(rejected["statusCode"], 401)
        self.assertNotIn(token_value, repr(self.repo.desktop_sync_tokens))


if __name__ == "__main__":
    unittest.main()
