from __future__ import annotations

import base64
import ipaddress
import logging
import mimetypes
import os
import re
import threading
import uuid
from pathlib import Path
from typing import Any, Callable, Iterable
from urllib.parse import parse_qs

from .app import ApiApplication, AppConfig
from .filesystem_store import FileSystemEconomyStore
from .postgres_repository import PostgresConnection, PostgresRepository
from .security import SecurityError


_LOG = logging.getLogger("aion2_economy_wsgi")
_LOCK = threading.Lock()
_APPLICATION: ApiApplication | None = None
_STATIC_ROOT = Path(os.environ.get("SPA_ROOT", "/opt/aion2-economy/web")).resolve()
_ITEM_ASSET_ROOT = Path(
    os.environ.get("ITEM_ASSET_ROOT", "/var/lib/aion2-economy/item-icons")
).resolve()
_ITEM_ASSET_PATH = re.compile(r"^/assets/(item-[0-9]+-[0-9a-f]{16}\.png)$")
_SNAPSHOT_ROOT = Path(
    os.environ.get("SNAPSHOT_ROOT", "/var/lib/aion2-economy/snapshots")
).resolve()
_CSP = (
    "default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
    "font-src 'self'; connect-src 'self'; form-action 'self'; base-uri 'none'; "
    "object-src 'none'; frame-ancestors 'none'"
)
_SECURITY_HEADERS = [
    ("Content-Security-Policy", _CSP),
    ("X-Frame-Options", "DENY"),
    ("X-Content-Type-Options", "nosniff"),
    ("Referrer-Policy", "no-referrer"),
    ("Permissions-Policy", "camera=(), microphone=(), geolocation=()"),
    ("Strict-Transport-Security", "max-age=31536000; includeSubDomains"),
]


def _decode_secret() -> bytes:
    value = os.environ.get("SESSION_HMAC_KEY_B64", "")
    try:
        return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
    except ValueError as exc:
        raise SecurityError("invalid_session_secret_encoding") from exc


def _build_application() -> ApiApplication:
    origins = tuple(
        value.strip()
        for value in os.environ.get("ALLOWED_ORIGINS", "").split(",")
        if value.strip()
    )
    connection = PostgresConnection(os.environ["POSTGRES_DSN"])
    return ApiApplication(
        PostgresRepository(connection),
        FileSystemEconomyStore(_SNAPSHOT_ROOT),
        _decode_secret(),
        AppConfig(
            allowed_origins=origins,
            registration_enabled=os.environ.get("REGISTRATION_ENABLED", "false").lower()
            == "true",
            write_fenced=os.environ.get("WRITE_FENCED", "false").lower() == "true",
            api_release_sha256=os.environ.get("API_RELEASE_SHA256"),
        ),
    )


def _application() -> ApiApplication:
    global _APPLICATION
    if _APPLICATION is None:
        with _LOCK:
            if _APPLICATION is None:
                _APPLICATION = _build_application()
    return _APPLICATION


def _safe_static_path(path: str) -> Path | None:
    # Resolve the root at request time as well as the candidate. Besides being
    # the correct traversal boundary, this keeps Windows drive/case aliases
    # from making an otherwise in-root path fail ``relative_to``.
    static_root = _STATIC_ROOT.resolve()
    item_asset_root = _ITEM_ASSET_ROOT.resolve()
    item_asset = _ITEM_ASSET_PATH.fullmatch(path)
    if item_asset is not None:
        candidate = (item_asset_root / item_asset.group(1)).resolve()
        try:
            candidate.relative_to(item_asset_root)
        except ValueError:
            return None
        if candidate.is_file() and not candidate.is_symlink():
            return candidate
        return None
    relative = path.removeprefix("/")
    candidate = (static_root / relative).resolve()
    try:
        candidate.relative_to(static_root)
    except ValueError:
        return None
    if candidate.is_file() and not candidate.is_symlink():
        return candidate
    if "." not in Path(relative).name:
        index = (static_root / "index.html").resolve()
        if index.is_file() and not index.is_symlink():
            return index
    return None


def _static_response(path: str) -> tuple[str, list[tuple[str, str]], bytes]:
    target = _safe_static_path(path)
    if target is None:
        return "404 Not Found", [("Content-Type", "text/plain; charset=utf-8")], b"not found"
    with target.open("rb") as stream:
        body = stream.read(8 * 1024 * 1024 + 1)
    if len(body) > 8 * 1024 * 1024:
        return "413 Payload Too Large", [("Content-Type", "text/plain")], b"too large"
    content_type = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
    cache = (
        "no-store"
        if target.name == "index.html"
        else "public, max-age=31536000, immutable"
    )
    return "200 OK", [("Content-Type", content_type), ("Cache-Control", cache)], body


def _event(environ: dict[str, Any], body: bytes) -> dict[str, Any]:
    headers: dict[str, str] = {}
    for key, value in environ.items():
        if key.startswith("HTTP_"):
            headers[key[5:].replace("_", "-").lower()] = str(value)
    if environ.get("CONTENT_TYPE"):
        headers["content-type"] = str(environ["CONTENT_TYPE"])
    if environ.get("CONTENT_ENCODING"):
        headers["content-encoding"] = str(environ["CONTENT_ENCODING"])
    query = {
        key: values[-1]
        for key, values in parse_qs(
            str(environ.get("QUERY_STRING", "")), keep_blank_values=True
        ).items()
        if values
    }
    path = str(environ.get("PATH_INFO") or "/")
    remote_address = str(environ.get("REMOTE_ADDR") or "unknown")
    forwarded = headers.get("x-forwarded-for", "")
    if remote_address in {"127.0.0.1", "::1"} and forwarded:
        candidate = forwarded.rsplit(",", maxsplit=1)[-1].strip()
        try:
            remote_address = str(ipaddress.ip_address(candidate))
        except ValueError:
            remote_address = "unknown"
    return {
        "httpMethod": str(environ.get("REQUEST_METHOD") or "GET"),
        "path": path,
        "rawPath": path,
        "headers": headers,
        "queryStringParameters": query,
        "body": base64.b64encode(body).decode("ascii") if body else "",
        "isBase64Encoded": bool(body),
        "requestContext": {
            "requestId": headers.get("x-request-id") or str(uuid.uuid4()),
            "identity": {"sourceIp": remote_address},
        },
    }


def application(
    environ: dict[str, Any], start_response: Callable[[str, list[tuple[str, str]]], Any]
) -> Iterable[bytes]:
    path = str(environ.get("PATH_INFO") or "/")
    try:
        if not path.startswith("/api/"):
            status, headers, body = _static_response(path)
            headers.extend(_SECURITY_HEADERS)
            start_response(status, headers)
            return [body]
        length = int(environ.get("CONTENT_LENGTH") or 0)
        if length < 0 or length > 1024 * 1024:
            start_response("413 Payload Too Large", [("Content-Type", "application/json")])
            return [b'{"error":"payload_too_large"}']
        body = environ["wsgi.input"].read(length) if length else b""
        response = _application().handle(_event(environ, body), None)
        status_code = int(response["statusCode"])
        reason = {
            200: "OK",
            201: "Created",
            204: "No Content",
            400: "Bad Request",
            401: "Unauthorized",
            403: "Forbidden",
            404: "Not Found",
            409: "Conflict",
            413: "Payload Too Large",
            429: "Too Many Requests",
            500: "Internal Server Error",
            503: "Service Unavailable",
        }.get(status_code, "Response")
        headers = [(str(key), str(value)) for key, value in response.get("headers", {}).items()]
        for key, values in response.get("multiValueHeaders", {}).items():
            headers.extend((str(key), str(value)) for value in values)
        headers.extend(_SECURITY_HEADERS)
        response_body = str(response.get("body") or "")
        encoded = (
            base64.b64decode(response_body, validate=True)
            if response.get("isBase64Encoded") is True
            else response_body.encode("utf-8")
        )
        start_response(f"{status_code} {reason}", headers)
        return [encoded]
    except Exception:
        # Database/provider exception text can contain connection metadata.
        # Keep the operational log useful without serialising exception data.
        _LOG.error("economy_wsgi_request_failed")
        start_response(
            "503 Service Unavailable",
            [("Content-Type", "application/json"), ("Cache-Control", "no-store")]
            + _SECURITY_HEADERS,
        )
        return [b'{"error":"dependency_unavailable"}']
