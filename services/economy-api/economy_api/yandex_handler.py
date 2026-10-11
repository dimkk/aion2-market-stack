from __future__ import annotations

import base64
import gzip
import json
import logging
import os
import re
import threading
from typing import Any

from .app import ApiApplication, AppConfig
from .object_store import IamObjectClient, ObjectStorageEconomyStore
from .security import SecurityError
from .ydb_repository import YdbConnection, YdbRepository

_lock = threading.Lock()
_application: ApiApplication | None = None
_spa_index: str | None = None
_LOG = logging.getLogger("aion2_economy_api")
_CSP = (
    "default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
    "font-src 'self'; connect-src 'self'; form-action 'self'; base-uri 'none'; "
    "object-src 'none'; frame-ancestors 'none'"
)
_HSTS = "max-age=31536000; includeSubDomains"
_API_JSON_GZIP_MIN_BYTES = 512 * 1024
_YANDEX_API_GATEWAY_RESPONSE_LIMIT_BYTES = 2_500_000
# Leave deterministic headroom below API Gateway's 2.5 MB response ceiling for
# the response envelope and headers. Base64 expands gzip bytes by roughly 4/3.
_GATEWAY_BASE64_BODY_BUDGET_BYTES = 2_000_000
_ASSET_PATH = re.compile(r"^/assets/([A-Za-z0-9][A-Za-z0-9._-]{0,159})$")
_SPA_PATH = re.compile(
    r"^/(?:overview|items(?:/[0-9]{1,16}(?:/[0-9]{1,16})?)?|"
    r"movers|servers|craft|farm|crawler|admin)/?$"
)
_CONTENT_HASHED_ASSET_NAME = re.compile(
    r"^[A-Za-z0-9][A-Za-z0-9._-]*-[A-Za-z0-9_-]{8,64}\.[A-Za-z0-9]+$"
)
_ASSET_CONTENT_TYPES = {
    ".css": "text/css; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".png": "image/png",
    ".webp": "image/webp",
    ".woff2": "font/woff2",
}


def _bootstrap_failure(event: dict[str, Any]) -> dict[str, Any]:
    context = event.get("requestContext") or {}
    request_id = str(context.get("requestId") or "unavailable")[:128]
    # Never attach exc_info here: provider/Lockbox/YDB exception strings may
    # include endpoint details or other sensitive deployment context.
    _LOG.error(
        "economy_api_event %s",
        json.dumps(
            {
                "event": "bootstrap_failed",
                "request_id": request_id,
                "outcome": "dependency_unavailable",
                "status": 503,
            },
            separators=(",", ":"),
        ),
    )
    return {
        "statusCode": 503,
        "headers": {
            "Content-Type": "application/json; charset=utf-8",
            "Cache-Control": "no-store",
            "X-Content-Type-Options": "nosniff",
            "Strict-Transport-Security": _HSTS,
        },
        "body": '{"error":"dependency_unavailable"}',
    }


def _decode_secret() -> bytes:
    value = os.environ.get("SESSION_HMAC_KEY_B64", "")
    try:
        return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
    except ValueError as exc:
        raise SecurityError("invalid_session_secret_encoding") from exc


def _bounded_api_response(response: dict[str, Any]) -> dict[str, Any]:
    headers = response.get("headers")
    body = response.get("body")
    if not isinstance(headers, dict) or not isinstance(body, str):
        return response
    normalized_headers = {str(key).lower(): str(value) for key, value in headers.items()}
    if (
        not normalized_headers.get("content-type", "").lower().startswith("application/json")
        or "content-encoding" in normalized_headers
        or response.get("isBase64Encoded") is True
    ):
        return response
    raw = body.encode("utf-8")
    if len(raw) < _API_JSON_GZIP_MIN_BYTES:
        return response
    compressed = gzip.compress(raw, compresslevel=6, mtime=0)
    encoded = base64.b64encode(compressed).decode("ascii")
    if len(encoded) > _GATEWAY_BASE64_BODY_BUDGET_BYTES:
        safe_headers = dict(headers)
        safe_headers["Content-Type"] = "application/json; charset=utf-8"
        safe_headers["Cache-Control"] = "no-store"
        safe_headers.pop("Content-Encoding", None)
        safe_headers.pop("Content-Length", None)
        return {
            "statusCode": 503,
            "headers": safe_headers,
            "body": '{"error":"response_too_large"}',
        }
    compressed_headers = dict(headers)
    compressed_headers["Content-Encoding"] = "gzip"
    vary = normalized_headers.get("vary")
    compressed_headers["Vary"] = (
        f"{vary}, Accept-Encoding" if vary and "accept-encoding" not in vary.lower()
        else vary or "Accept-Encoding"
    )
    return {
        **response,
        "headers": compressed_headers,
        "isBase64Encoded": True,
        "body": encoded,
    }


def _build_application(*, ready_timeout: float = 8) -> ApiApplication:
    endpoint = os.environ["YDB_ENDPOINT"]
    database = os.environ["YDB_DATABASE"]
    origins = tuple(
        value.strip()
        for value in os.environ.get("ALLOWED_ORIGINS", "").split(",")
        if value.strip()
    )
    registration_enabled = os.environ.get("REGISTRATION_ENABLED", "false").lower() == "true"
    write_fenced = os.environ.get("WRITE_FENCED", "false").lower() == "true"
    connection = YdbConnection(endpoint, database, ready_timeout=ready_timeout)
    return ApiApplication(
        YdbRepository(connection),
        ObjectStorageEconomyStore(os.environ["SNAPSHOT_BUCKET"]),
        _decode_secret(),
        AppConfig(
            allowed_origins=origins,
            registration_enabled=registration_enabled,
            write_fenced=write_fenced,
            api_release_sha256=os.environ.get("API_RELEASE_SHA256"),
        ),
    )


def handler(event: dict[str, Any], context: object) -> dict[str, Any]:
    global _application, _spa_index
    try:
        request_context = event.get("requestContext") or {}
        http = request_context.get("http") or {}
        method = str(event.get("httpMethod") or http.get("method") or "GET").upper()
        path = str(event.get("path") or event.get("rawPath") or http.get("path") or "/")
        if method == "GET" and (path == "/" or _SPA_PATH.fullmatch(path) is not None):
            if _spa_index is None:
                response = IamObjectClient().get_object(
                    Bucket=os.environ["SPA_BUCKET"], Key="index.html"
                )
                raw = response["Body"].read(1_048_577)
                if not raw or len(raw) > 1_048_576:
                    raise RuntimeError("spa_index_invalid")
                _spa_index = raw.decode("utf-8")
            return {
                "statusCode": 200,
                "headers": {
                    "Content-Type": "text/html; charset=utf-8",
                    "Cache-Control": "no-store",
                    "Content-Security-Policy": _CSP,
                    "X-Frame-Options": "DENY",
                    "X-Content-Type-Options": "nosniff",
                    "Referrer-Policy": "no-referrer",
                    "Permissions-Policy": "camera=(), microphone=(), geolocation=()",
                    "Strict-Transport-Security": _HSTS,
                },
                "body": _spa_index,
            }
        asset_match = _ASSET_PATH.fullmatch(path) if method == "GET" else None
        if asset_match is not None:
            asset_name = asset_match.group(1)
            extension = os.path.splitext(asset_name)[1].lower()
            content_type = _ASSET_CONTENT_TYPES.get(extension)
            if (
                content_type is None
                or _CONTENT_HASHED_ASSET_NAME.fullmatch(asset_name) is None
            ):
                return {
                    "statusCode": 404,
                    "headers": {
                        "Content-Type": "application/json; charset=utf-8",
                        "Cache-Control": "no-store",
                        "X-Content-Type-Options": "nosniff",
                        "Strict-Transport-Security": _HSTS,
                    },
                    "body": '{"error":"not_found"}',
                }
            response = IamObjectClient().get_object(
                Bucket=os.environ["SPA_BUCKET"], Key=f"assets/{asset_name}"
            )
            raw = response["Body"].read(8_388_609)
            if not raw or len(raw) > 8_388_608:
                raise RuntimeError("spa_asset_invalid")
            return {
                "statusCode": 200,
                "headers": {
                    "Content-Type": content_type,
                    "Cache-Control": "public, max-age=31536000, immutable",
                    "X-Content-Type-Options": "nosniff",
                    "Strict-Transport-Security": _HSTS,
                },
                "isBase64Encoded": True,
                "body": base64.b64encode(raw).decode("ascii"),
            }
        if not path.startswith("/api/"):
            return {
                "statusCode": 404,
                "headers": {
                    "Content-Type": "application/json; charset=utf-8",
                    "Cache-Control": "no-store",
                    "X-Content-Type-Options": "nosniff",
                    "Strict-Transport-Security": _HSTS,
                },
                "body": '{"error":"not_found"}',
            }
        if _application is None:
            with _lock:
                if _application is None:
                    _application = _build_application()
        response = _application.handle(event, context)
        return _bounded_api_response(response) if path.startswith("/api/") else response
    except Exception:
        return _bootstrap_failure(event)
