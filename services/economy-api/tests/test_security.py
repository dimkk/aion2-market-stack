from __future__ import annotations

import unittest
import base64
import hashlib
from datetime import UTC, datetime
from unittest.mock import patch

from economy_api.security import (
    CSRF_COOKIE,
    DUMMY_PASSWORD_HASH,
    DUMMY_LEGACY_PASSWORD_HASH,
    SESSION_COOKIE,
    create_session,
    decode_session_cookie,
    hash_password,
    normalize_username,
    parse_cookie_header,
    password_hash_needs_rehash,
    verify_password,
    verify_login_password,
)


class SecurityTests(unittest.TestCase):
    def test_missing_user_dummy_uses_the_current_scrypt_profile(self) -> None:
        self.assertTrue(DUMMY_PASSWORD_HASH.startswith("scrypt$131072$8$1$"))
        self.assertFalse(password_hash_needs_rehash(DUMMY_PASSWORD_HASH))
        self.assertTrue(
            verify_password("dummy-password-never-authenticates", DUMMY_PASSWORD_HASH)
        )

    def test_scrypt_round_trip_and_malformed_verifier(self) -> None:
        encoded = hash_password("correct horse battery staple", salt=b"0123456789abcdef")
        self.assertTrue(encoded.startswith("scrypt$131072$8$1$"))
        self.assertTrue(verify_password("correct horse battery staple", encoded))
        self.assertFalse(verify_password("wrong password", encoded))
        self.assertFalse(verify_password("anything", "not-a-verifier"))

    def test_legacy_scrypt_verifies_but_requires_rehash(self) -> None:
        password = "correct horse battery staple"
        salt = b"0123456789abcdef"
        derived = hashlib.scrypt(
            password.encode("utf-8"), salt=salt, n=1 << 14, r=8, p=1, dklen=32
        )
        encode = lambda value: base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")
        legacy = f"scrypt$16384$8$1${encode(salt)}${encode(derived)}"
        self.assertTrue(verify_password(password, legacy))
        self.assertTrue(password_hash_needs_rehash(legacy))
        self.assertFalse(password_hash_needs_rehash(hash_password(password, salt=salt)))

    def test_login_verifier_executes_current_and_legacy_profiles_for_every_account_shape(self) -> None:
        current = hash_password("correct horse battery staple", salt=b"0123456789abcdef")
        for encoded in (None, current, DUMMY_LEGACY_PASSWORD_HASH, "malformed"):
            observed: list[str] = []

            def record(_password: object, verifier: str) -> bool:
                observed.append(verifier.split("$", 3)[1])
                return verifier == encoded

            with patch("economy_api.security.verify_password", side_effect=record):
                verify_login_password("correct horse battery staple", encoded)
            self.assertEqual(observed, ["131072", "16384"])

    def test_session_is_opaque_signed_and_tamper_evident(self) -> None:
        secret = b"s" * 32
        created = create_session(
            "alice", secret, now=datetime(2026, 10, 5, tzinfo=UTC)
        )
        self.assertNotIn("alice", created.cookie_value)
        self.assertEqual(
            decode_session_cookie(created.cookie_value, secret),
            created.record.session_hash,
        )
        self.assertIsNone(decode_session_cookie(created.cookie_value + "x", secret))

    def test_cookie_parser_does_not_confuse_session_and_csrf(self) -> None:
        cookies = parse_cookie_header(f"{SESSION_COOKIE}=a.b; {CSRF_COOKIE}=csrf")
        self.assertEqual(cookies[SESSION_COOKIE], "a.b")
        self.assertEqual(cookies[CSRF_COOKIE], "csrf")

    def test_username_is_normalized_and_constrained(self) -> None:
        self.assertEqual(normalize_username(" Alice_1 "), "alice_1")
        with self.assertRaises(ValueError):
            normalize_username("../../admin")


if __name__ == "__main__":
    unittest.main()
