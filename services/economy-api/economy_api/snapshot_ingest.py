from __future__ import annotations

import gzip
import hashlib
import io
import json
import math
import re
from typing import Any


UPLOAD_SCHEMA = "aion2.economy.view-upload/v1"
COMMIT_SCHEMA = "aion2.economy.views-commit/v1"
RECEIPT_SCHEMA = "aion2.economy.views-publication-receipt/v1"
SNAPSHOT_SCHEMA = "aion2.economy.snapshot/v1"
POINTER_SCHEMA = "aion2.economy.pointer/v1"
MAX_VIEW_BYTES = 8 * 1024 * 1024
MAX_UPLOAD_DOCUMENT_BYTES = MAX_VIEW_BYTES + 128 * 1024
MAX_COMMIT_DOCUMENT_BYTES = 128 * 1024
MAX_WIRE_BYTES = 1024 * 1024
MAX_VIEWS = 256

REVISION_RE = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
VIEW_NAME_RE = re.compile(
    r"^(market|items|movers|servers|craft|farm-value)\.([a-z0-9_-]{1,48})\.json$"
)
JWT_RE = re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{8,}\b")
BEARER_RE = re.compile(r"\bBearer\s+[A-Za-z0-9._~+/=-]{12,}", re.IGNORECASE)
SERVICE_TOKEN_RE = re.compile(r"\ba2(?:fs|farm|cl)_[A-Za-z0-9_-]{20,}\b", re.IGNORECASE)
SENSITIVE_KEYS = frozenset(
    {
        "authorization",
        "cookie",
        "cookies",
        "password",
        "password_hash",
        "secret",
        "secrets",
        "jwt",
        "access_token",
        "refresh_token",
        "session_token",
        "session_cookie",
        "request_headers",
        "response_headers",
        "raw_payload",
        "raw_capture",
        "pcap",
        "tls_keys",
        "tls_keylog",
        "payload_hex",
        "payload_bytes",
        "source_path",
        "destination_path",
        "filesystem_path",
        "local_path",
        "capture_path",
    }
)
SENSITIVE_KEY_TOKENS = frozenset(
    re.sub(r"[^a-z0-9]", "", key.lower()) for key in SENSITIVE_KEYS
)
ALLOWED_DOCUMENT_KEYS = frozenset(
    {
        "schemaVersion", "revision", "generatedAt", "build", "stats", "items",
        "catalog", "recipes", "servers", "farmSessions", "marketRules",
        "limitations", "segments", "itemCount", "up", "down", "new", "liquid",
        "listings", "activeItems", "catalogItems", "coverageItems", "category",
        "change1h", "change24h", "change7d", "current", "dimensionSource",
        "dimensionsConfirmed", "en", "faction", "fee", "grade", "history",
        "iconName", "iconUrl", "internalName", "itemId", "itemLevel",
        "lastObservedAt", "liquidityScore", "listingCount", "market", "materials",
        "median", "minimum", "name", "names", "newestActiveObservedAt",
        "newlyLiquid", "newlyLiquidAt", "oldestActiveObservedAt", "outputItemId",
        "outputName", "outputNames", "outputQuantity", "quantity", "recipeId",
        "region", "ru", "saleCount", "saleTaxRate", "saleTaxSource", "serverId",
        "serverRates", "source", "subcategory", "subtab", "tier", "value", "at",
    }
)
REQUIRED_DEFAULT_VIEWS = frozenset(
    {
        "market.default.json",
        "items.default.json",
        "movers.default.json",
        "servers.default.json",
        "craft.default.json",
        "farm-value.default.json",
    }
)


class SnapshotIngestError(ValueError):
    pass


def canonical_json(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def decode_request_document(
    body: bytes,
    *,
    content_encoding: str,
    maximum_document_bytes: int,
) -> dict[str, Any]:
    if len(body) > MAX_WIRE_BYTES:
        raise SnapshotIngestError("snapshot_wire_too_large")
    encoding = content_encoding.strip().lower()
    if encoding not in {"", "identity", "gzip"}:
        raise SnapshotIngestError("snapshot_content_encoding_invalid")
    try:
        if encoding == "gzip":
            with gzip.GzipFile(fileobj=io.BytesIO(body), mode="rb") as stream:
                decoded = stream.read(maximum_document_bytes + 1)
        else:
            decoded = body
    except (OSError, EOFError) as exc:
        raise SnapshotIngestError("snapshot_gzip_invalid") from exc
    if not decoded or len(decoded) > maximum_document_bytes:
        raise SnapshotIngestError("snapshot_document_size_invalid")
    try:
        value = json.loads(
            decoded,
            parse_constant=lambda _value: (_ for _ in ()).throw(
                SnapshotIngestError("snapshot_number_invalid")
            ),
        )
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError) as exc:
        raise SnapshotIngestError("snapshot_json_invalid") from exc
    if not isinstance(value, dict):
        raise SnapshotIngestError("snapshot_json_invalid")
    return value


def _scan_sanitized(value: Any, *, depth: int = 0) -> None:
    if depth > 32:
        raise SnapshotIngestError("snapshot_nesting_too_deep")
    if isinstance(value, dict):
        for key, child in value.items():
            if not isinstance(key, str):
                raise SnapshotIngestError("snapshot_key_invalid")
            if (
                key not in ALLOWED_DOCUMENT_KEYS
                or re.sub(r"[^a-z0-9]", "", key.lower()) in SENSITIVE_KEY_TOKENS
            ):
                raise SnapshotIngestError("snapshot_sensitive_field_rejected")
            _scan_sanitized(child, depth=depth + 1)
        return
    if isinstance(value, list):
        for child in value:
            _scan_sanitized(child, depth=depth + 1)
        return
    if isinstance(value, str):
        if JWT_RE.search(value) or BEARER_RE.search(value) or SERVICE_TOKEN_RE.search(value):
            raise SnapshotIngestError("snapshot_sensitive_value_rejected")
        return
    if isinstance(value, float) and not math.isfinite(value):
        raise SnapshotIngestError("snapshot_number_invalid")
    if value is not None and not isinstance(value, (str, int, float, bool)):
        raise SnapshotIngestError("snapshot_value_invalid")


def _validate_view_shape(name: str, document: dict[str, Any]) -> None:
    view = VIEW_NAME_RE.fullmatch(name)
    if view is None:
        raise SnapshotIngestError("snapshot_view_name_invalid")
    common = {"schemaVersion", "revision", "generatedAt", "build"}
    group, segment = view.groups()
    if group == "market":
        allowed = (
            common
            | {
                "stats", "items", "catalog", "recipes", "servers", "farmSessions",
                "marketRules", "limitations", "segments",
            }
            if segment == "default"
            else common | {"items"}
        )
    elif group == "items":
        allowed = common | {"items", "itemCount", "segments"}
        if segment != "default":
            allowed = common | {"items"}
    elif group == "movers":
        allowed = common | {"up", "down", "new", "liquid", "listings"}
    elif group == "servers":
        allowed = common | {"servers"}
    elif group == "craft":
        allowed = common | {"recipes", "marketRules"}
    else:
        allowed = common | {"farmSessions", "marketRules", "limitations"}
    if not set(document).issubset(allowed):
        raise SnapshotIngestError("snapshot_view_shape_invalid")
    field = {
        "market": "items",
        "items": "items",
        "servers": "servers",
        "craft": "recipes",
        "farm-value": "farmSessions",
    }.get(group)
    if field is not None and not isinstance(document.get(field), list):
        raise SnapshotIngestError("snapshot_view_shape_invalid")
    if group == "movers" and any(
        not isinstance(document.get(field_name), list)
        for field_name in ("up", "down", "new", "liquid", "listings")
    ):
        raise SnapshotIngestError("snapshot_view_shape_invalid")


def validate_view_upload(value: dict[str, Any]) -> tuple[str, str, bytes, str]:
    if set(value) != {"schema", "revision", "name", "sha256", "document"}:
        raise SnapshotIngestError("snapshot_upload_schema_invalid")
    revision = value.get("revision")
    name = value.get("name")
    digest = value.get("sha256")
    document = value.get("document")
    if (
        value.get("schema") != UPLOAD_SCHEMA
        or not isinstance(revision, str)
        or REVISION_RE.fullmatch(revision) is None
        or not isinstance(name, str)
        or VIEW_NAME_RE.fullmatch(name) is None
        or not isinstance(digest, str)
        or SHA256_RE.fullmatch(digest) is None
        or not isinstance(document, dict)
        or document.get("schemaVersion") != SNAPSHOT_SCHEMA
        or document.get("revision") != revision
    ):
        raise SnapshotIngestError("snapshot_upload_schema_invalid")
    _scan_sanitized(document)
    _validate_view_shape(name, document)
    encoded = canonical_json(document)
    if not encoded or len(encoded) > MAX_VIEW_BYTES:
        raise SnapshotIngestError("snapshot_view_size_invalid")
    if sha256_bytes(encoded) != digest:
        raise SnapshotIngestError("snapshot_view_hash_mismatch")
    return revision, name, encoded, digest


def commit_payload_digest(value: dict[str, Any]) -> str:
    return sha256_bytes(canonical_json(value))


def validate_commit(
    value: dict[str, Any],
) -> tuple[str, str | None, dict[str, str], str]:
    if set(value) != {"schema", "revision", "expectedCurrentRevision", "views"}:
        raise SnapshotIngestError("snapshot_commit_schema_invalid")
    revision = value.get("revision")
    expected = value.get("expectedCurrentRevision")
    views = value.get("views")
    if (
        value.get("schema") != COMMIT_SCHEMA
        or not isinstance(revision, str)
        or REVISION_RE.fullmatch(revision) is None
        or (
            expected is not None
            and (not isinstance(expected, str) or REVISION_RE.fullmatch(expected) is None)
        )
        or not isinstance(views, dict)
        or not 1 <= len(views) <= MAX_VIEWS
    ):
        raise SnapshotIngestError("snapshot_commit_schema_invalid")
    normalized: dict[str, str] = {}
    for name, digest in views.items():
        if (
            not isinstance(name, str)
            or VIEW_NAME_RE.fullmatch(name) is None
            or not isinstance(digest, str)
            or SHA256_RE.fullmatch(digest) is None
        ):
            raise SnapshotIngestError("snapshot_commit_schema_invalid")
        normalized[name] = digest
    if not REQUIRED_DEFAULT_VIEWS.issubset(normalized):
        raise SnapshotIngestError("snapshot_required_views_missing")
    canonical = {
        "schema": COMMIT_SCHEMA,
        "revision": revision,
        "expectedCurrentRevision": expected,
        "views": dict(sorted(normalized.items())),
    }
    return revision, expected, normalized, commit_payload_digest(canonical)


def view_upload_idempotency_key(
    revision: str, name: str, digest: str
) -> str:
    return sha256_bytes(
        canonical_json({"revision": revision, "name": name, "sha256": digest})
    )
