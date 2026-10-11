from __future__ import annotations

import base64
import hashlib
import hmac
import re
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from http.cookies import SimpleCookie

from .models import DesktopSyncToken, Session

SESSION_COOKIE = "__Host-a2e_session"
CSRF_COOKIE = "__Host-a2e_csrf"
_USERNAME_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{2,63}$")
_SCRYPT_N = 1 << 17
_SCRYPT_R = 8
_SCRYPT_P = 1
_SCRYPT_DKLEN = 32
_SCRYPT_MAXMEM = 256 * 1024 * 1024
_LEGACY_SCRYPT_PROFILES = frozenset({(1 << 14, 8, 1)})
_DESKTOP_TOKEN_RE = re.compile(r"^a2fs_([a-f0-9]{32})_([A-Za-z0-9_-]{43})$")

# A fixed verifier for the missing-user login path.  It is deliberately not a
# credential: the application rejects the request after doing the same current
# scrypt work as it would for an existing user.  Keep its work-factor profile in
# sync with ``hash_password`` so username existence is not exposed by the
# legacy-vs-current scrypt cost difference.
DUMMY_PASSWORD_HASH = (
    "scrypt$131072$8$1$MDAwMDAwMDAwMDAwMDAwMA$"
    "UJgkhtNSzY4ByFzMSUmSV2bCNJKloKVMTrC1AvZ8y-g"
)
DUMMY_LEGACY_PASSWORD_HASH = (
    "scrypt$16384$8$1$MTExMTExMTExMTExMTExMQ$"
    "LZJQtZ8Z7H9878Dzxc95By68NHCmmON7KEr4d1MW8Pw"
)


class SecurityError(ValueError):
    pass


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _unb64(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def normalize_username(value: object) -> str:
    if not isinstance(value, str):
        raise SecurityError("invalid_username")
    username = value.strip().lower()
    if not _USERNAME_RE.fullmatch(username):
        raise SecurityError("invalid_username")
    return username


def validate_password(value: object) -> str:
    if not isinstance(value, str):
        raise SecurityError("invalid_password")
    encoded = value.encode("utf-8")
    if len(encoded) < 12 or len(encoded) > 256:
        raise SecurityError("invalid_password")
    return value


def hash_password(password: str, *, salt: bytes | None = None) -> str:
    validate_password(password)
    salt = salt or secrets.token_bytes(16)
    derived = hashlib.scrypt(
        password.encode("utf-8"),
        salt=salt,
        n=_SCRYPT_N,
        r=_SCRYPT_R,
        p=_SCRYPT_P,
        dklen=_SCRYPT_DKLEN,
        maxmem=_SCRYPT_MAXMEM,
    )
    return f"scrypt${_SCRYPT_N}${_SCRYPT_R}${_SCRYPT_P}${_b64(salt)}${_b64(derived)}"


def verify_password(password: object, encoded: str) -> bool:
    try:
        if not isinstance(password, str):
            return False
        kind, n, r, p, salt, expected = encoded.split("$", 5)
        if kind != "scrypt":
            return False
        n_i, r_i, p_i = int(n), int(r), int(p)
        profile = (n_i, r_i, p_i)
        if profile != (_SCRYPT_N, _SCRYPT_R, _SCRYPT_P) and profile not in _LEGACY_SCRYPT_PROFILES:
            return False
        actual = hashlib.scrypt(
            password.encode("utf-8"),
            salt=_unb64(salt),
            n=n_i,
            r=r_i,
            p=p_i,
            dklen=len(_unb64(expected)),
            maxmem=_SCRYPT_MAXMEM,
        )
        return hmac.compare_digest(actual, _unb64(expected))
    except (ValueError, TypeError):
        return False


def _password_profile(encoded: str | None) -> tuple[int, int, int] | None:
    try:
        if encoded is None:
            return None
        kind, n, r, p, _salt, _expected = encoded.split("$", 5)
        if kind != "scrypt":
            return None
        return int(n), int(r), int(p)
    except (ValueError, TypeError):
        return None


def verify_login_password(password: object, encoded: str | None) -> bool:
    """Verify a login with one current and one legacy scrypt operation.

    The same work-profile sequence is executed for missing, current, legacy and
    malformed account records. This closes the legacy-account timing oracle
    without silently dropping the supported one-login migration path.
    """

    profile = _password_profile(encoded)
    current_profile = (_SCRYPT_N, _SCRYPT_R, _SCRYPT_P)
    legacy_profile = next(iter(_LEGACY_SCRYPT_PROFILES))
    current_encoded = encoded if profile == current_profile else DUMMY_PASSWORD_HASH
    legacy_encoded = encoded if profile == legacy_profile else DUMMY_LEGACY_PASSWORD_HASH
    current_valid = verify_password(password, current_encoded)
    legacy_valid = verify_password(password, legacy_encoded)
    if profile == current_profile:
        return current_valid
    if profile == legacy_profile:
        return legacy_valid
    return False


def password_hash_needs_rehash(encoded: str) -> bool:
    try:
        kind, n, r, p, _salt, _expected = encoded.split("$", 5)
        return kind != "scrypt" or (int(n), int(r), int(p)) != (
            _SCRYPT_N, _SCRYPT_R, _SCRYPT_P
        )
    except (ValueError, TypeError):
        return True


def require_session_secret(value: bytes) -> bytes:
    if len(value) < 32:
        raise SecurityError("session_secret_too_short")
    return value


def digest_token(value: str, secret: bytes) -> str:
    return hmac.new(secret, value.encode("ascii"), hashlib.sha256).hexdigest()


@dataclass(frozen=True, slots=True)
class NewSession:
    record: Session
    cookie_value: str
    csrf_value: str


@dataclass(frozen=True, slots=True)
class NewDesktopSyncToken:
    record: DesktopSyncToken
    token_value: str


def create_session(
    username: str,
    secret: bytes,
    *,
    now: datetime | None = None,
    ttl: timedelta = timedelta(hours=12),
) -> NewSession:
    require_session_secret(secret)
    now = now or datetime.now(UTC)
    opaque = _b64(secrets.token_bytes(32))
    signature = _b64(hmac.new(secret, opaque.encode("ascii"), hashlib.sha256).digest())
    csrf = _b64(secrets.token_bytes(24))
    record = Session(
        session_hash=digest_token(opaque, secret),
        username=username,
        csrf_hash=digest_token(csrf, secret),
        created_at=now,
        expires_at=now + ttl,
    )
    return NewSession(record=record, cookie_value=f"{opaque}.{signature}", csrf_value=csrf)


def decode_session_cookie(value: str, secret: bytes) -> str | None:
    try:
        opaque, provided = value.split(".", 1)
        if len(opaque) < 40 or len(provided) < 40:
            return None
        expected = _b64(hmac.new(secret, opaque.encode("ascii"), hashlib.sha256).digest())
        if not hmac.compare_digest(provided, expected):
            return None
        return digest_token(opaque, secret)
    except (ValueError, UnicodeError):
        return None


def create_desktop_sync_token(
    username: str,
    secret: bytes,
    *,
    now: datetime,
    ttl: timedelta,
    label: str | None,
    scope: str = "farm:write",
) -> NewDesktopSyncToken:
    """Create a one-time-visible farm-write credential.

    Only ``token_id`` and the keyed digest in ``record`` are persisted.  The
    random bearer value exists only in this return value and the HTTPS response
    that immediately follows issuance.
    """
    require_session_secret(secret)
    token_id = secrets.token_hex(16)
    opaque = _b64(secrets.token_bytes(32))
    token_value = f"a2fs_{token_id}_{opaque}"
    record = DesktopSyncToken(
        token_id=token_id,
        token_digest=digest_token(token_value, secret),
        owner_username=username,
        scope=scope,
        label=label,
        created_at=now,
        expires_at=now + ttl,
        revoked_at=None,
    )
    return NewDesktopSyncToken(record=record, token_value=token_value)


def desktop_sync_token_id(value: str) -> str | None:
    match = _DESKTOP_TOKEN_RE.fullmatch(value)
    return match.group(1) if match else None


def parse_cookie_header(value: str | None) -> dict[str, str]:
    if not value:
        return {}
    cookie = SimpleCookie()
    try:
        cookie.load(value)
    except Exception:
        return {}
    return {key: morsel.value for key, morsel in cookie.items()}


def session_set_cookie(value: str, max_age: int) -> str:
    return (
        f"{SESSION_COOKIE}={value}; Path=/; Max-Age={max_age}; "
        "Secure; HttpOnly; SameSite=Strict"
    )


def csrf_set_cookie(value: str, max_age: int) -> str:
    return f"{CSRF_COOKIE}={value}; Path=/; Max-Age={max_age}; Secure; SameSite=Strict"


def expired_cookies() -> list[str]:
    suffix = "Path=/; Max-Age=0; Secure; SameSite=Strict"
    return [
        f"{SESSION_COOKIE}=; {suffix}; HttpOnly",
        f"{CSRF_COOKIE}=; {suffix}",
    ]


def pseudonym(value: str, secret: bytes) -> str:
    return hmac.new(secret, value.encode("utf-8"), hashlib.sha256).hexdigest()[:24]
