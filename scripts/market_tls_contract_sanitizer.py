#!/usr/bin/env python3
"""Recover a bounded AION2 market HTTP contract inside the private analyzer.

Input is a ZIP with exactly ``capture.pcapng`` and ``tls.keys``.  The TLS key
log and decrypted HTTP are consumed only in a private temporary directory.
Only the allowlisted market authority, request contract, safe JSON fields and
listing values leave the analyzer.  Header values, cookies, bearer material,
account/player/seller values and raw bodies never enter the report.
"""

from __future__ import annotations

import argparse
import base64
from collections import defaultdict, deque
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
from typing import Any
from urllib.parse import parse_qsl, urlsplit
import zipfile


FORMAT = "aion2_market_tls_contract_v1"
AUTHORITY = "qos-api.g.nc.com"
ARCHIVE_MEMBERS = frozenset({"capture.pcapng", "tls.keys"})
MAX_CAPTURE_BYTES = 256 * 1024 * 1024
MAX_KEYS_BYTES = 4 * 1024 * 1024
MAX_TSHARK_JSON_BYTES = 256 * 1024 * 1024
MAX_TLS_FOLLOW_BYTES = 256 * 1024 * 1024
MAX_TRANSACTIONS = 200
MAX_BODY_BYTES = 4 * 1024 * 1024
MAX_REPORT_BYTES = 2 * 1024 * 1024
MAX_LATE_ATTACH_STREAMS = 16
MAX_LATE_ATTACH_SEQUENCE = 131072
MAX_LATE_ATTACH_AEAD_TRIALS = 64_000_000
MAX_LATE_ATTACH_RECORD_GAP = 64

HEADER_NAME = re.compile(r"^[!#$%&'*+.^_`|~0-9A-Za-z-]{1,128}$")
SECRET_HEADER = re.compile(
    r"(?:authorization|proxy-authorization|cookie|set-cookie|token|ticket|session|"
    r"credential|password|secret|signature|nonce|device)", re.IGNORECASE
)
SAFE_LITERAL_HEADER = re.compile(
    r"^(?:accept|accept-encoding|accept-language|content-type|content-encoding|"
    r"user-agent|x-[a-z0-9-]*(?:app|build|client|game|platform|region|version)[a-z0-9-]*)$",
    re.IGNORECASE,
)
DENY_VALUE_KEY = re.compile(
    r"(?:account|character|player|seller|owner|user|member|guild|email|phone|auth|"
    r"token|ticket|cookie|session|credential|password|secret|signature|nonce|device)",
    re.IGNORECASE,
)
SAFE_VALUE_KEY = re.compile(
    r"(?:^|_)(?:app_id|item(?:_id|_ids|_key|_name)?|product(?:_id)?|goods(?:_id)?|"
    r"trade_server_id|server_id|world_id|region|scope|market|category|categories|grade|level|"
    r"page|page_size|offset|limit|cursor|sort|order|sale_id|listing_id|price|"
    r"unit_price|total_price|amount|quantity|count|currency|fee|fee_rate|fee_type|remain(?:ing)?|expire|"
    r"expiry|effective_from|effective_to|duration|timestamp|created_at|updated_at|"
    r"result|status|code)(?:$|_)", re.IGNORECASE
)
SAFE_TEXT = re.compile(r"^[\w .,:+\-/]{0,160}$", re.UNICODE)
SAFE_MARKET_SORT_KEYS = frozenset({"UnitPrice", "SalePrice", "EffectiveTo"})
SAFE_MARKET_SORT_ORDERS = frozenset({"ASC", "DESC"})
SAFE_MARKET_FILTER_KEY = re.compile(r"^[A-Za-z][A-Za-z0-9_.:+-]{0,63}$")
SAFE_MARKET_FILTER_VALUE = re.compile(r"^[A-Za-z0-9_.:+\-/]{1,80}$")
# NSS key-log labels include a numeric epoch suffix for TLS 1.3 application
# traffic secrets (for example CLIENT_TRAFFIC_SECRET_0).
KEYLOG_LINE = re.compile(r"^[A-Z0-9_]+ [0-9A-Fa-f]+ [0-9A-Fa-f]+$")
HTTP1_REQUEST = re.compile(
    rb"(?:GET|POST|PUT|PATCH|DELETE|HEAD|OPTIONS) [^\r\n ]{1,2048} HTTP/1\.[01]\r\n"
)
HTTP1_RESPONSE = re.compile(rb"HTTP/1\.[01] [1-5][0-9]{2}(?: [^\r\n]*)?\r\n")
SAFE_ERROR_CODES = frozenset({
    "invalid_tls_bundle",
    "invalid_tls_bundle_members",
    "capture_size_out_of_range",
    "keylog_size_out_of_range",
    "archive_member_size_mismatch",
    "invalid_tls_keylog",
    "market_tls_dissector_failed",
    "market_tls_dissector_timeout",
    "tls_random_probe_failed",
    "tls_diagnostics_failed",
    "tshark_output_invalid",
    "invalid_tshark_json",
    "unexpected_http_body_field",
    "unsafe_http_path",
    "unsafe_market_tls_contract",
    "market_tls_report_too_large",
    "market_tls_io_failed",
    "market_tls_analysis_failed",
})


def _canonical_key(value: str) -> str:
    value = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", value)
    return re.sub(r"[^a-z0-9]+", "_", value.lower()).strip("_")


def _one(value: Any) -> Any:
    if isinstance(value, list):
        return value[-1] if value else None
    return value


def _many(value: Any) -> list[Any]:
    if isinstance(value, list):
        return value
    return [] if value is None else [value]


def _flatten_layers(packet: Any) -> dict[str, Any]:
    if not isinstance(packet, dict):
        return {}
    source = packet.get("_source")
    if not isinstance(source, dict) or not isinstance(source.get("layers"), dict):
        return {}
    result: dict[str, Any] = {}
    for protocol in source["layers"].values():
        if isinstance(protocol, dict):
            result.update(protocol)
        elif isinstance(protocol, list):
            for item in protocol:
                if isinstance(item, dict):
                    result.update(item)
    return result


def _decode_body(value: Any) -> bytes:
    value = _one(value)
    if value is None:
        return b""
    if not isinstance(value, str):
        raise ValueError("unexpected_http_body_field")
    compact = value.replace(":", "").replace(" ", "")
    if compact and len(compact) % 2 == 0 and re.fullmatch(r"[0-9A-Fa-f]+", compact):
        return bytes.fromhex(compact)
    return value.encode("utf-8", errors="strict")


def _safe_scalar(key: str, value: Any, context: str = "") -> Any:
    canonical = _canonical_key(key)
    if DENY_VALUE_KEY.search(canonical):
        return "<redacted>"
    if (context == "game_item_conditions" and canonical in {"key", "type", "value"}
            and isinstance(value, str)
            and re.fullmatch(r"[A-Za-z0-9_.:+\-/]{1,160}", value)):
        return value
    if context == "market_sort" and isinstance(value, str):
        if canonical == "key" and value in SAFE_MARKET_SORT_KEYS:
            return value
        if canonical == "order_by" and value in SAFE_MARKET_SORT_ORDERS:
            return value
        return "<redacted>"
    if context == "market_search" and isinstance(value, str):
        if canonical == "key" and SAFE_MARKET_FILTER_KEY.fullmatch(value):
            return value
        if canonical == "value" and SAFE_MARKET_FILTER_VALUE.fullmatch(value):
            return value
        return "<redacted>"
    if (canonical == "categories" and isinstance(value, str)
            and re.fullmatch(r"[0-9,]{1,160}", value)):
        return value
    if not SAFE_VALUE_KEY.search(canonical):
        return f"<{type(value).__name__}>"
    if value is None or type(value) in (bool, int, float):
        return value
    if isinstance(value, str) and SAFE_TEXT.fullmatch(value):
        return value
    return "<redacted>"


def safe_shape(value: Any, key: str = "", depth: int = 0,
               context: str = "") -> Any:
    if depth >= 8:
        return "<depth-limit>"
    if isinstance(value, dict):
        result: dict[str, Any] = {}
        canonical_key = _canonical_key(key)
        child_context = {
            "game_item_conditions": "game_item_conditions",
            "sort": "market_sort",
            "search": "market_search",
        }.get(canonical_key, context)
        for child_key in sorted(value, key=lambda item: str(item))[:200]:
            name = str(child_key)[:160]
            child = value[child_key]
            result[name] = ("<redacted>" if DENY_VALUE_KEY.search(_canonical_key(name))
                            else safe_shape(child, name, depth + 1, child_context))
        if len(value) > 200:
            result["<truncated-keys>"] = len(value) - 200
        return result
    if isinstance(value, list):
        canonical_key = _canonical_key(key)
        list_context = {
            "game_item_conditions": "game_item_conditions",
            "search": "market_search",
        }.get(canonical_key, context)
        return {
            "type": "array",
            "count": len(value),
            "sample": [safe_shape(item, key, depth + 1, list_context)
                       for item in value[:5]],
        }
    return _safe_scalar(key, value, context)


def _body_contract(body: bytes, content_type: str) -> dict[str, Any]:
    result: dict[str, Any] = {
        "content_type": content_type.split(";", 1)[0].strip().lower(),
        "bytes": len(body),
        "encoding": "empty" if not body else "opaque",
        "shape": None,
    }
    if not body:
        return result
    if len(body) > MAX_BODY_BYTES:
        result["encoding"] = "oversize"
        return result
    if result["content_type"] in {
        "application/json", "text/json", "application/problem+json"
    } or body[:1] in (b"{", b"["):
        try:
            decoded = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            result["encoding"] = "invalid_json"
        else:
            result["encoding"] = "json"
            result["shape"] = safe_shape(decoded)
    return result


def _static_materials(path: Path | None) -> dict[str, str]:
    if path is None or not path.is_file():
        return {}
    document = json.loads(path.read_text(encoding="utf-8"))
    records = document.get("records", []) if isinstance(document, dict) else []
    result: dict[str, str] = {}
    for record in records:
        if not isinstance(record, dict):
            continue
        value, name = record.get("value"), record.get("name")
        if isinstance(value, str) and value and isinstance(name, str):
            result[value] = f"{document.get('client_version', 'unknown')}:{name}"
    return result


def _value_class(value: str) -> str:
    if value.lower().startswith("bearer "):
        token = value[7:].strip()
        return "bearer_jwt" if token.count(".") == 2 else "bearer_opaque"
    if value.lower().startswith("basic "):
        return "basic"
    if re.fullmatch(r"[0-9A-Fa-f-]{32,64}", value):
        return "identifier_or_digest"
    if len(value) >= 32:
        return "opaque"
    return "literal"


def _header_safe_metadata(name: str, value: str) -> dict[str, Any]:
    metadata: dict[str, Any] = {}
    if name == "authorization":
        scheme = value.split(None, 1)[0] if value else ""
        if re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]{0,31}", scheme):
            metadata["scheme"] = scheme.lower()
    if name not in {"client-context", "client-game-context"}:
        return metadata
    raw: bytes | None = None
    encoding = "opaque"
    if value[:1] in "[{":
        raw = value.encode("utf-8")
        encoding = "json"
    elif len(value) <= 32 * 1024 and re.fullmatch(r"[A-Za-z0-9_+/=-]+", value):
        try:
            padded = value + "=" * (-len(value) % 4)
            raw = base64.urlsafe_b64decode(padded.encode("ascii"))
            encoding = "base64"
        except (ValueError, UnicodeError):
            raw = None
    if raw is not None and len(raw) <= 16 * 1024:
        try:
            decoded = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            pass
        else:
            metadata["structured_encoding"] = (
                "json" if encoding == "json" else "base64_json"
            )
            metadata["structured_shape"] = safe_shape(decoded)
            return metadata
    metadata["structured_encoding"] = encoding
    return metadata


def _credential_components(value: str) -> set[str]:
    """Return private comparison candidates without exporting credential bytes."""
    components = {value}
    parts = value.split(None, 1)
    credential = parts[1] if len(parts) == 2 else value
    components.add(credential)
    jwt_parts = credential.split(".")
    if len(jwt_parts) == 3:
        for segment in jwt_parts[:2]:
            try:
                padded = segment + "=" * (-len(segment) % 4)
                decoded = json.loads(base64.urlsafe_b64decode(
                    padded.encode("ascii")
                ).decode("utf-8"))
            except (ValueError, UnicodeError, json.JSONDecodeError):
                continue
            stack = [decoded]
            while stack:
                item = stack.pop()
                if isinstance(item, dict):
                    stack.extend(item.values())
                elif isinstance(item, list):
                    stack.extend(item)
                elif isinstance(item, str) and item:
                    components.add(item)
                elif type(item) in (int, float, bool):
                    components.add(str(item))
    return components


def _jwt_safe_metadata(value: str) -> dict[str, Any]:
    """Describe a JWT without exporting claims, timestamps, or token bytes."""
    parts = value.split(None, 1)
    credential = parts[1] if len(parts) == 2 else value
    segments = credential.split(".")
    if len(segments) != 3:
        return {}

    decoded: list[Any] = []
    for segment in segments[:2]:
        try:
            padded = segment + "=" * (-len(segment) % 4)
            decoded.append(json.loads(base64.urlsafe_b64decode(
                padded.encode("ascii")
            ).decode("utf-8")))
        except (ValueError, UnicodeError, json.JSONDecodeError):
            return {"jwt_decodable": False}
    header, claims = decoded
    if not isinstance(header, dict) or not isinstance(claims, dict):
        return {"jwt_decodable": False}

    def type_name(item: Any) -> str:
        if item is None:
            return "null"
        if type(item) is bool:
            return "bool"
        if type(item) is int:
            return "int"
        if type(item) is float:
            return "float"
        if isinstance(item, str):
            return "str"
        if isinstance(item, list):
            return "array"
        if isinstance(item, dict):
            return "object"
        return "other"

    claim_types = {
        str(key)[:96]: type_name(claims[key])
        for key in sorted(claims, key=lambda item: str(item))[:128]
        if re.fullmatch(r"[A-Za-z0-9_.:-]{1,96}", str(key))
    }
    metadata: dict[str, Any] = {
        "jwt_decodable": True,
        "jwt_claim_types": claim_types,
        "jwt_has_signature": bool(segments[2]),
    }
    algorithm = header.get("alg")
    token_type = header.get("typ")
    if isinstance(algorithm, str) and re.fullmatch(r"[A-Za-z0-9_-]{1,32}", algorithm):
        metadata["jwt_alg"] = algorithm
    if isinstance(token_type, str) and re.fullmatch(r"[A-Za-z0-9_.+-]{1,32}", token_type):
        metadata["jwt_typ"] = token_type
    issued, expires = claims.get("iat"), claims.get("exp")
    if (type(issued) is int and type(expires) is int
            and 0 <= expires - issued <= 7 * 24 * 60 * 60):
        metadata["jwt_lifetime_seconds"] = expires - issued
    return metadata


def _header_contract(lines: Any, static: dict[str, str]) -> list[dict[str, Any]]:
    if isinstance(lines, str):
        lines = [lines]
    if not isinstance(lines, list):
        return []
    result = []
    for line in lines:
        if not isinstance(line, str) or ":" not in line:
            continue
        name, value = line.split(":", 1)
        name, value = name.strip().lower(), value.strip().rstrip("\r\n")
        if not HEADER_NAME.fullmatch(name):
            continue
        row: dict[str, Any] = {
            "name": name,
            "value_exported": False,
            "value_source": "runtime_or_unknown",
            "value_class": _value_class(value),
            **_header_safe_metadata(name, value),
        }
        if value in static:
            row["value_source"] = "client_embedded_static"
            row["static_material_ref"] = static[value]
        elif name in {"host", "content-length", "connection"}:
            row["value_source"] = "protocol_derived"
        elif SECRET_HEADER.search(name):
            row["value_source"] = "runtime_session"
        elif SAFE_LITERAL_HEADER.fullmatch(name) and len(value) <= 160 and SAFE_TEXT.fullmatch(value):
            row["value_exported"] = True
            row["value"] = value
            row["value_source"] = "protocol_literal"
        result.append(row)
    return sorted(result, key=lambda row: row["name"])


def _http2_header_lines(fields: dict[str, Any]) -> list[str]:
    names = _many(fields.get("http2.header.name"))
    values = _many(fields.get("http2.header.value"))
    return [
        f"{name}: {value}"
        for name, value in zip(names, values)
        if isinstance(name, str) and isinstance(value, str) and not name.startswith(":")
    ]


def _request_target(uri: str) -> tuple[str, dict[str, Any]]:
    parsed = urlsplit(uri)
    path = parsed.path or "/"
    if not path.startswith("/") or len(path) > 2048 or any(ch in path for ch in "\r\n"):
        raise ValueError("unsafe_http_path")
    path = re.sub(
        r"^/sessions/v1\.0/[0-9A-Fa-f-]{32,64}$",
        "/sessions/v1.0/{session_id}",
        path,
    )
    path = re.sub(
        r"^/storage/v1\.0/buckets/[0-9A-Fa-f-]{32,64}/objects/"
        r"character\.[0-9]+\.[0-9]+$",
        "/storage/v1.0/buckets/{bucket_id}/objects/"
        "character.{game_server_id}.{char_key}",
        path,
    )
    path = re.sub(
        r"^/logs/v1\.0/[0-9A-Fa-f-]{32,64}/logs$",
        "/logs/v1.0/{log_session_id}/logs",
        path,
    )
    query: dict[str, Any] = {}
    for name, value in parse_qsl(parsed.query, keep_blank_values=True):
        canonical = _canonical_key(name)
        if DENY_VALUE_KEY.search(canonical):
            query[name] = "<redacted>"
        elif SAFE_VALUE_KEY.search(canonical) and len(value) <= 160 and SAFE_TEXT.fullmatch(value):
            query[name] = value
        else:
            query[name] = "<redacted>"
    return path, query


def _decode_chunked(data: bytes, start: int) -> tuple[bytes, int] | None:
    cursor = start
    result = bytearray()
    while True:
        line_end = data.find(b"\r\n", cursor)
        if line_end < 0 or line_end - cursor > 32:
            return None
        size_text = data[cursor:line_end].split(b";", 1)[0]
        try:
            size = int(size_text, 16)
        except ValueError:
            return None
        cursor = line_end + 2
        if size < 0 or len(result) + size > MAX_BODY_BYTES or cursor + size + 2 > len(data):
            return None
        result.extend(data[cursor:cursor + size])
        cursor += size
        if data[cursor:cursor + 2] != b"\r\n":
            return None
        cursor += 2
        if size == 0:
            trailer_end = data.find(b"\r\n\r\n", cursor)
            return bytes(result), (trailer_end + 4 if trailer_end >= 0 else cursor)


def _http1_messages(data: bytes, request: bool) -> list[dict[str, Any]]:
    pattern = HTTP1_REQUEST if request else HTTP1_RESPONSE
    messages: list[dict[str, Any]] = []
    cursor = 0
    while len(messages) < MAX_TRANSACTIONS:
        match = pattern.search(data, cursor)
        if match is None:
            break
        header_end = data.find(b"\r\n\r\n", match.start())
        if header_end < 0 or header_end - match.start() > 64 * 1024:
            break
        try:
            lines = data[match.start():header_end].decode("iso-8859-1").split("\r\n")
        except UnicodeError:
            cursor = match.end()
            continue
        headers: dict[str, str] = {}
        header_lines: list[str] = []
        for line in lines[1:]:
            if ":" not in line:
                continue
            name, value = line.split(":", 1)
            name = name.strip().lower()
            if HEADER_NAME.fullmatch(name):
                headers[name] = value.strip()
                header_lines.append(f"{name}: {value.strip()}")
        body_start = header_end + 4
        end = body_start
        body = b""
        if "chunked" in headers.get("transfer-encoding", "").lower():
            decoded = _decode_chunked(data, body_start)
            if decoded is None:
                cursor = match.end()
                continue
            body, end = decoded
        else:
            try:
                length = int(headers.get("content-length", "0"))
            except ValueError:
                cursor = match.end()
                continue
            if length < 0 or length > MAX_BODY_BYTES or body_start + length > len(data):
                cursor = match.end()
                continue
            body = data[body_start:body_start + length]
            end = body_start + length
        messages.append({
            "first_line": lines[0],
            "headers": headers,
            "header_lines": header_lines,
            "body": body,
        })
        cursor = max(end, match.end())
    return messages


def _parse_http1_fallback(
    streams: dict[str, dict[str, bytearray]], static: dict[str, str]
) -> tuple[list[dict[str, Any]], int]:
    transactions: list[dict[str, Any]] = []
    private_rows: list[tuple[dict[str, Any], dict[str, str], bytes]] = []
    message_count = 0
    for stream in sorted(streams):
        requests = _http1_messages(bytes(streams[stream]["request"]), True)
        responses = _http1_messages(bytes(streams[stream]["response"]), False)
        message_count += len(requests) + len(responses)
        for index, request in enumerate(requests):
            host = request["headers"].get("host", "").lower().rstrip(".")
            if host != AUTHORITY or len(transactions) >= MAX_TRANSACTIONS:
                continue
            parts = request["first_line"].split(" ", 2)
            if len(parts) != 3:
                continue
            path, query = _request_target(parts[1])
            transaction = {
                "method": parts[0].upper(),
                "path": path,
                "query": query,
                "headers": _header_contract(request["header_lines"], static),
                "body": _body_contract(
                    request["body"], request["headers"].get("content-type", "")
                ),
                "response": None,
            }
            if index < len(responses):
                response = responses[index]
                status_parts = response["first_line"].split(" ", 2)
                transaction["response"] = {
                    "status_code": int(status_parts[1]),
                    "body": _body_contract(
                        response["body"], response["headers"].get("content-type", "")
                    ),
                }
                private_rows.append((transaction, request["headers"], response["body"]))
            else:
                private_rows.append((transaction, request["headers"], b""))
            transactions.append(transaction)
    bootstrap_headers: dict[str, set[str]] = defaultdict(set)
    issued_credentials: dict[str, set[str]] = defaultdict(set)
    correlated_names = {
        "authorization", "client-context", "client-game-context",
        "device-tag", "user-agent",
    }
    for transaction, headers, response_body in private_rows:
        if not transaction["path"].startswith("/sessions/v1.0/"):
            continue
        if transaction["path"] == "/sessions/v1.0/authn_token":
            for name in correlated_names:
                if headers.get(name):
                    bootstrap_headers[name].add(headers[name])
        try:
            payload = json.loads(response_body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            continue
        if isinstance(payload, dict):
            for field, role in (
                ("authn_token", "issued_authn_token"),
                ("user_market_code", "issued_user_market_code"),
                ("user_credential_code", "issued_user_credential_code"),
            ):
                credential = payload.get(field)
                if isinstance(credential, str) and credential:
                    issued_credentials[role].add(credential)
    credential_aliases: dict[str, str] = {}
    for transaction, headers, _response_body in private_rows:
        for row in transaction["headers"]:
            name = row["name"]
            value = headers.get(name, "")
            if name in correlated_names:
                row["matches_session_authn_request"] = bool(
                    value and value in bootstrap_headers.get(name, set())
                )
                components = _credential_components(value) if value else set()
                matched_issued_roles = sorted(
                    role for role, credentials in issued_credentials.items()
                    if components & credentials
                )
                if matched_issued_roles:
                    row["issued_credential_roles"] = matched_issued_roles
            if name == "authorization":
                parts = value.split(None, 1)
                credential = parts[1] if len(parts) == 2 else ""
                if credential and credential not in credential_aliases:
                    credential_aliases[credential] = f"J{len(credential_aliases) + 1:03d}"
                if credential:
                    row["credential_instance"] = credential_aliases[credential]
                    row.update(_jwt_safe_metadata(value))
                matched_role = next(iter(row.get("issued_credential_roles", [])), None)
                if matched_role is not None:
                    row["credential_role"] = matched_role
                elif value and value in bootstrap_headers.get(name, set()):
                    row["credential_role"] = "session_bootstrap_jwt"
                else:
                    row["credential_role"] = "other_or_unknown"
    return transactions, message_count


def parse_tshark_packets(packets: Any, static: dict[str, str] | None = None,
                         keylog_match: dict[str, Any] | None = None,
                         follow_transactions: list[dict[str, Any]] | None = None,
                         follow_message_count: int = 0) -> dict[str, Any]:
    if not isinstance(packets, list):
        raise ValueError("invalid_tshark_json")
    static = static or {}
    pending: dict[str, deque[dict[str, Any]]] = defaultdict(deque)
    transactions: list[dict[str, Any]] = []
    by_stream: dict[str, dict[str, Any]] = {}
    plaintext_streams: dict[str, dict[str, bytearray]] = defaultdict(
        lambda: {"request": bytearray(), "response": bytearray()}
    )
    decrypted_http_messages = 0
    for packet in packets:
        fields = _flatten_layers(packet)
        is_http2 = fields.get("http2.streamid") is not None
        method = _one(fields.get("http.request.method")) or _one(
            fields.get("http2.headers.method")
        )
        status = _one(fields.get("http.response.code")) or _one(
            fields.get("http2.headers.status")
        )
        tcp_stream = str(_one(fields.get("tcp.stream")) or "")
        stream = (f"{tcp_stream}:{_one(fields.get('http2.streamid'))}"
                  if is_http2 else tcp_stream)
        host = str(_one(fields.get("http.host")) or _one(
            fields.get("http2.headers.authority")
        ) or "").lower().rstrip(".")
        body_field = (fields.get("http.file_data")
                      or fields.get("http2.body.reassembled.data")
                      or fields.get("http2.data.data"))
        content_type = str(_one(fields.get("http.content_type")) or _one(
            fields.get("http2.headers.content_type")
        ) or "")
        data_value = fields.get("data.data")
        if data_value is not None and tcp_stream:
            try:
                source_port = int(str(_one(fields.get("tcp.srcport")) or "0"))
                destination_port = int(str(_one(fields.get("tcp.dstport")) or "0"))
                plaintext = _decode_body(data_value)
            except (TypeError, ValueError):
                plaintext = b""
                source_port = destination_port = 0
            if destination_port == 443:
                plaintext_streams[tcp_stream]["request"].extend(plaintext)
            elif source_port == 443:
                plaintext_streams[tcp_stream]["response"].extend(plaintext)
        if method:
            decrypted_http_messages += 1
            if host != AUTHORITY or len(transactions) >= MAX_TRANSACTIONS:
                continue
            uri = str(_one(fields.get("http.request.uri")) or _one(
                fields.get("http2.headers.path")
            ) or "/")
            path, query = _request_target(uri)
            body = _decode_body(body_field)
            transaction = {
                "method": str(method).upper(),
                "path": path,
                "query": query,
                "headers": _header_contract(
                    fields.get("http.request.line") if not is_http2
                    else _http2_header_lines(fields),
                    static,
                ),
                "body": _body_contract(body, content_type),
                "response": None,
            }
            transactions.append(transaction)
            pending[stream].append(transaction)
            by_stream[stream] = transaction
        elif status:
            decrypted_http_messages += 1
            if not pending[stream]:
                continue
            transaction = pending[stream].popleft()
            body = _decode_body(body_field)
            transaction["response"] = {
                "status_code": int(status),
                "body": _body_contract(body, content_type),
            }
            by_stream[stream] = transaction
        elif is_http2 and body_field is not None and stream in by_stream:
            body = _decode_body(body_field)
            transaction = by_stream[stream]
            if transaction.get("response") is None:
                transaction["body"] = _body_contract(body, content_type)
            else:
                transaction["response"]["body"] = _body_contract(body, content_type)
    if not transactions and plaintext_streams:
        transactions, fallback_messages = _parse_http1_fallback(plaintext_streams, static)
        decrypted_http_messages += fallback_messages
    if not transactions and follow_transactions:
        transactions = follow_transactions[:MAX_TRANSACTIONS]
        decrypted_http_messages += follow_message_count

    keylog_match = keylog_match or {
        "handshakes_seen": 0,
        "keylog_sessions_available": 0,
        "keylog_session_matches": 0,
        "keylog_application_sessions_available": 0,
        "matching_application_sessions": 0,
        "encrypted_application_frames": 0,
        "recognized_application_frames": 0,
        "application_protocols": [],
        "tcp_gap_frames": 0,
        "tcp_out_of_order_frames": 0,
        "tls_reassembly_error_frames": 0,
        "tls_follow_streams_seen": 0,
        "tls_follow_streams_with_plaintext": 0,
        "tls_follow_plaintext_bytes": 0,
        "tls_follow_http_messages": 0,
        "tls_follow_probe_failures": 0,
        "tls_follow_stream_failures": 0,
        "late_attach_streams_seen": 0,
        "late_attach_streams_decrypted": 0,
        "late_attach_plaintext_bytes": 0,
        "late_attach_http_messages": 0,
        "late_attach_aead_trials": 0,
    }
    report = {
        "format": FORMAT,
        "authority": AUTHORITY,
        "tls": {
            "decrypted_http_messages": decrypted_http_messages,
            "ephemeral_keylog_used": True,
            "key_bytes_exported": False,
            **keylog_match,
        },
        "transactions": transactions,
        "privacy": {
            "raw_capture_exported": False,
            "raw_body_exported": False,
            "header_values_exported_only_when_safe": True,
            "credentials_exported": False,
            "account_player_seller_values_exported": False,
            "unsafe_fields_exported": False,
        },
    }
    return validate(report)


def _keylog_match_counts(capture: Path, keys: Path) -> dict[str, int]:
    completed = subprocess.run([
        "tshark", "-n", "-r", str(capture),
        "-Y", "tls.handshake.type == 1",
        "-T", "fields", "-e", "tls.handshake.random",
    ], check=False, capture_output=True, timeout=60)
    if completed.returncode or len(completed.stdout) > 4 * 1024 * 1024:
        raise ValueError("tls_random_probe_failed")
    seen = {
        value.replace(":", "").lower()
        for line in completed.stdout.decode("ascii", errors="strict").splitlines()
        for value in line.split(",")
        if re.fullmatch(r"[0-9A-Fa-f:]{64,95}", value)
        and len(value.replace(":", "")) == 64
    }
    available = set()
    labels_by_session: dict[str, set[str]] = defaultdict(set)
    for line in keys.read_text(encoding="ascii", errors="strict").splitlines():
        parts = line.strip().split()
        if len(parts) == 3 and parts[0] != "RSA" and len(parts[1]) == 64:
            session = parts[1].lower()
            available.add(session)
            labels_by_session[session].add(parts[0])
    application_ready = {
        session
        for session, labels in labels_by_session.items()
        if "CLIENT_RANDOM" in labels or {
            "CLIENT_TRAFFIC_SECRET_0", "SERVER_TRAFFIC_SECRET_0"
        }.issubset(labels)
    }
    return {
        "handshakes_seen": len(seen),
        "keylog_sessions_available": len(available),
        "keylog_session_matches": len(seen & available),
        "keylog_application_sessions_available": len(application_ready),
        "matching_application_sessions": len(seen & application_ready),
    }


def _tls_decode_diagnostics(capture: Path, keys: Path) -> dict[str, Any]:
    base = [
        "tshark", "-n", "-r", str(capture),
        "-o", f"tls.keylog_file:{keys}",
    ]
    application = subprocess.run(base + [
        "-Y", "tls.app_data or tls.app_data_proto",
        "-T", "fields", "-E", "separator=/t",
        "-e", "frame.number", "-e", "tls.app_data_proto",
    ], check=False, capture_output=True, timeout=120)
    quality = subprocess.run(base + [
        "-Y", "tcp.analysis.lost_segment or tcp.analysis.out_of_order or tls.segment.error",
        "-T", "fields", "-E", "separator=/t",
        "-e", "frame.number", "-e", "tcp.analysis.lost_segment",
        "-e", "tcp.analysis.out_of_order", "-e", "tls.segment.error",
    ], check=False, capture_output=True, timeout=120)
    if (application.returncode or quality.returncode
            or len(application.stdout) > 4 * 1024 * 1024
            or len(quality.stdout) > 4 * 1024 * 1024):
        raise ValueError("tls_diagnostics_failed")

    application_rows = [
        line.split("\t", 1)
        for line in application.stdout.decode("utf-8", errors="strict").splitlines()
        if line.strip()
    ]
    protocols: set[str] = set()
    recognized = 0
    for row in application_rows:
        value = row[1].lower() if len(row) > 1 else ""
        if not value:
            continue
        recognized += 1
        protocols.update(re.findall(r"[a-z0-9_.+-]{1,64}", value))

    lost = out_of_order = tls_error = 0
    for line in quality.stdout.decode("utf-8", errors="strict").splitlines():
        columns = line.split("\t")
        lost += int(len(columns) > 1 and bool(columns[1]))
        out_of_order += int(len(columns) > 2 and bool(columns[2]))
        tls_error += int(len(columns) > 3 and bool(columns[3]))
    return {
        "encrypted_application_frames": len(application_rows),
        "recognized_application_frames": recognized,
        "application_protocols": sorted(protocols),
        "tcp_gap_frames": lost,
        "tcp_out_of_order_frames": out_of_order,
        "tls_reassembly_error_frames": tls_error,
    }


def _parse_tls_follow_raw(output: bytes) -> tuple[bytes, bytes]:
    """Return request/response bytes from private ``follow,tls,raw`` output."""
    node_ports: dict[int, int] = {}
    chunks = {0: bytearray(), 1: bytearray()}
    for line in output.splitlines():
        node = re.fullmatch(rb"Node ([01]): .*:([0-9]{1,5})", line)
        if node:
            node_ports[int(node.group(1))] = int(node.group(2))
            continue
        direction = int(line.startswith(b"\t"))
        value = line[1:] if direction else line
        if (value and len(value) % 2 == 0
                and re.fullmatch(rb"[0-9A-Fa-f]+", value)):
            chunks[direction].extend(bytes.fromhex(value.decode("ascii")))
    if node_ports.get(0) == 443:
        return bytes(chunks[1]), bytes(chunks[0])
    if node_ports.get(1) == 443:
        return bytes(chunks[0]), bytes(chunks[1])
    return b"", b""


def _tls13_records(data: bytes) -> list[tuple[bytes, bytes]]:
    """Extract complete TLS records from a mid-stream TCP byte sequence."""
    records: list[tuple[bytes, bytes]] = []
    cursor = 0
    while cursor + 5 <= len(data) and len(records) < 4096:
        length = int.from_bytes(data[cursor + 3:cursor + 5], "big")
        if (data[cursor] not in (20, 21, 22, 23)
                or data[cursor + 1:cursor + 3] != b"\x03\x03"
                or not 1 <= length <= 18432
                or cursor + 5 + length > len(data)):
            cursor += 1
            continue
        header = data[cursor:cursor + 5]
        records.append((header, data[cursor + 5:cursor + 5 + length]))
        cursor += 5 + length
    return records


def _tls13_keylog_pairs(keys: Path) -> list[dict[str, Any]]:
    pairs: dict[tuple[str, int], dict[str, Any]] = {}
    pattern = re.compile(r"^(CLIENT|SERVER)_TRAFFIC_SECRET_([0-9]+)$")
    for line in keys.read_text(encoding="ascii", errors="strict").splitlines():
        parts = line.strip().split()
        if len(parts) != 3:
            continue
        match = pattern.fullmatch(parts[0])
        if not match or len(parts[1]) != 64:
            continue
        try:
            secret = bytes.fromhex(parts[2])
        except ValueError:
            continue
        if len(secret) not in (32, 48):
            continue
        key = (parts[1].lower(), int(match.group(2)))
        row = pairs.setdefault(key, {
            "session": key[0], "generation": key[1],
        })
        row[match.group(1).lower()] = secret
    return [row for row in reversed(list(pairs.values())) if "client" in row]


def _tls13_suite_material(secret: bytes) -> list[dict[str, Any]]:
    try:
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM, ChaCha20Poly1305
        from cryptography.hazmat.primitives.kdf.hkdf import HKDFExpand
    except ImportError as error:
        raise ValueError("tls_diagnostics_failed") from error

    if len(secret) == 48:
        specifications = [("aes256gcm-sha384", hashes.SHA384(), 32, AESGCM)]
    elif len(secret) == 32:
        specifications = [
            ("aes128gcm-sha256", hashes.SHA256(), 16, AESGCM),
            ("chacha20poly1305-sha256", hashes.SHA256(), 32, ChaCha20Poly1305),
        ]
    else:
        return []

    def expand(label: bytes, length: int, algorithm: Any) -> bytes:
        full = b"tls13 " + label
        info = (length.to_bytes(2, "big") + bytes([len(full)]) + full
                + b"\x00")
        return HKDFExpand(
            algorithm=algorithm, length=length, info=info,
        ).derive(secret)

    return [{
        "name": name,
        "aead": factory(expand(b"key", key_length, algorithm)),
        "iv": expand(b"iv", 12, algorithm),
    } for name, algorithm, key_length, factory in specifications]


def _tls13_plaintext(aead: Any, iv: bytes, sequence: int,
                     header: bytes, ciphertext: bytes) -> tuple[bytes, int] | None:
    nonce = bytearray(iv)
    encoded = sequence.to_bytes(8, "big")
    for index, value in enumerate(encoded, start=4):
        nonce[index] ^= value
    try:
        plaintext = aead.decrypt(bytes(nonce), ciphertext, header)
    except Exception:
        return None
    end = len(plaintext) - 1
    while end >= 0 and plaintext[end] == 0:
        end -= 1
    if end < 0 or plaintext[end] not in (20, 21, 22, 23):
        return None
    return plaintext[:end], plaintext[end]


def _tls13_match_client_secret(
    records: list[tuple[bytes, bytes]], pairs: list[dict[str, Any]],
    budget: dict[str, int],
) -> tuple[dict[str, Any], dict[str, Any], int] | None:
    application = next((record for record in records if record[0][0] == 23), None)
    if application is None:
        return None
    header, ciphertext = application
    # A long-lived market connection is often older than unrelated launcher,
    # telemetry and CDN sessions appended later to the same NSS key log.  A
    # pair-major scan can therefore burn the entire budget on newer, wrong
    # secrets.  Search low record sequences across every candidate first; the
    # correct older secret is then found without multiplying a full 131k scan
    # by every newer connection.
    candidates = [
        (pair, suite)
        for pair in pairs
        for suite in _tls13_suite_material(pair["client"])
    ]
    for sequence in range(MAX_LATE_ATTACH_SEQUENCE):
        for pair, suite in candidates:
            if budget["trials"] >= MAX_LATE_ATTACH_AEAD_TRIALS:
                return None
            budget["trials"] += 1
            if _tls13_plaintext(
                suite["aead"], suite["iv"], sequence, header, ciphertext
            ) is not None:
                return pair, suite, sequence
    return None


def _tls13_match_one_secret(
    record: tuple[bytes, bytes], secret: bytes, suite_name: str,
    budget: dict[str, int],
) -> tuple[dict[str, Any], int] | None:
    for suite in _tls13_suite_material(secret):
        if suite["name"] != suite_name:
            continue
        for sequence in range(MAX_LATE_ATTACH_SEQUENCE):
            if budget["trials"] >= MAX_LATE_ATTACH_AEAD_TRIALS:
                return None
            budget["trials"] += 1
            if _tls13_plaintext(
                suite["aead"], suite["iv"], sequence, record[0], record[1]
            ) is not None:
                return suite, sequence
    return None


def _tls13_decrypt_records(
    records: list[tuple[bytes, bytes]], suite: dict[str, Any], sequence: int,
) -> bytes:
    output = bytearray()
    current = sequence
    started = False
    for header, ciphertext in records:
        if header[0] != 23:
            continue
        decoded = None
        used = current
        for candidate in range(current, current + MAX_LATE_ATTACH_RECORD_GAP + 1):
            decoded = _tls13_plaintext(
                suite["aead"], suite["iv"], candidate, header, ciphertext
            )
            if decoded is not None:
                used = candidate
                break
        if decoded is None:
            if started:
                break
            continue
        started = True
        current = used + 1
        plaintext, inner_type = decoded
        if inner_type == 23:
            output.extend(plaintext)
            if len(output) > MAX_TLS_FOLLOW_BYTES:
                raise ValueError("tls_diagnostics_failed")
    return bytes(output)


def _tls13_late_attach_plaintext(
    capture: Path, keys: Path, stop_when_request_contains: bytes | None = None,
) -> tuple[dict[str, dict[str, bytearray]], dict[str, int]]:
    base = ["tshark", "-n", "-r", str(capture)]
    probe = subprocess.run(base + [
        "-Y", "tcp.port == 443 and tcp.len > 0", "-T", "fields",
        "-e", "tcp.stream",
    ], check=False, capture_output=True, timeout=120)
    if probe.returncode or len(probe.stdout) > 4 * 1024 * 1024:
        raise ValueError("tls_diagnostics_failed")
    stream_ids = sorted({
        int(value)
        for line in probe.stdout.decode("ascii", errors="strict").splitlines()
        for value in line.split(",")
        if value.isdigit() and 0 <= int(value) < 4096
    }, reverse=stop_when_request_contains is not None)[:MAX_LATE_ATTACH_STREAMS]
    pairs = _tls13_keylog_pairs(keys)
    budget = {"trials": 0}
    plaintext_streams: dict[str, dict[str, bytearray]] = {}
    matched = 0
    plaintext_bytes = 0
    for stream_id in stream_ids:
        followed = subprocess.run(base + [
            "-q", "-z", f"follow,tcp,raw,{stream_id}",
        ], check=False, capture_output=True, timeout=120)
        if followed.returncode or len(followed.stdout) > MAX_TLS_FOLLOW_BYTES:
            continue
        request_raw, response_raw = _parse_tls_follow_raw(followed.stdout)
        request_records = _tls13_records(request_raw)
        response_records = _tls13_records(response_raw)
        match = _tls13_match_client_secret(request_records, pairs, budget)
        if match is None:
            if budget["trials"] >= MAX_LATE_ATTACH_AEAD_TRIALS:
                break
            continue
        pair, client_suite, client_sequence = match
        request = _tls13_decrypt_records(
            request_records, client_suite, client_sequence
        )
        if (b"host: qos-api.g.nc.com" not in request.lower()
                and b":authority\r\nqos-api.g.nc.com" not in request.lower()):
            continue
        if stop_when_request_contains is not None:
            size = len(request)
            plaintext_bytes += size
            if plaintext_bytes > MAX_TLS_FOLLOW_BYTES:
                raise ValueError("tls_diagnostics_failed")
            plaintext_streams[str(stream_id)] = {
                "request": bytearray(request), "response": bytearray(),
            }
            matched += 1
            if stop_when_request_contains in request:
                break
            continue
        response = b""
        first_server = next(
            (record for record in response_records if record[0][0] == 23), None
        )
        if first_server is not None and "server" in pair:
            server_match = _tls13_match_one_secret(
                first_server, pair["server"], client_suite["name"], budget
            )
            if server_match is not None:
                server_suite, server_sequence = server_match
                response = _tls13_decrypt_records(
                    response_records, server_suite, server_sequence
                )
        size = len(request) + len(response)
        plaintext_bytes += size
        if plaintext_bytes > MAX_TLS_FOLLOW_BYTES:
            raise ValueError("tls_diagnostics_failed")
        plaintext_streams[str(stream_id)] = {
            "request": bytearray(request), "response": bytearray(response),
        }
        matched += 1
    return plaintext_streams, {
        "late_attach_streams_seen": len(stream_ids),
        "late_attach_streams_decrypted": matched,
        "late_attach_plaintext_bytes": plaintext_bytes,
        "late_attach_http_messages": 0,
        "late_attach_aead_trials": budget["trials"],
    }


def _tls13_late_attach_http1(
    capture: Path, keys: Path, static: dict[str, str],
) -> tuple[list[dict[str, Any]], int, dict[str, int]]:
    plaintext_streams, diagnostics = _tls13_late_attach_plaintext(capture, keys)
    transactions, messages = _parse_http1_fallback(plaintext_streams, static)
    diagnostics["late_attach_http_messages"] = messages
    return transactions, messages, diagnostics


def _tls_follow_plaintext(
    capture: Path, keys: Path,
) -> tuple[dict[str, dict[str, bytearray]], dict[str, int]]:
    base = [
        "tshark", "-n", "-r", str(capture),
        "-o", f"tls.keylog_file:{keys}",
        "-o", "tcp.desegment_tcp_streams:TRUE",
        "-o", "tls.desegment_ssl_records:TRUE",
        "-o", "tls.desegment_ssl_application_data:TRUE",
    ]
    probe = subprocess.run(base + [
        "-Y", "tls.app_data", "-T", "fields", "-e", "tcp.stream",
    ], check=False, capture_output=True, timeout=120)
    if len(probe.stdout) > 4 * 1024 * 1024:
        raise ValueError("tls_diagnostics_failed")
    if probe.returncode:
        return {}, {
            "tls_follow_streams_seen": 0,
            "tls_follow_streams_with_plaintext": 0,
            "tls_follow_plaintext_bytes": 0,
            "tls_follow_http_messages": 0,
            "tls_follow_probe_failures": 1,
            "tls_follow_stream_failures": 0,
        }
    stream_ids = sorted({
        int(value)
        for line in probe.stdout.decode("ascii", errors="strict").splitlines()
        for value in line.split(",")
        if value.isdigit() and 0 <= int(value) < 4096
    })[:128]
    plaintext_streams: dict[str, dict[str, bytearray]] = {}
    plaintext_bytes = 0
    stream_failures = 0
    for stream_id in stream_ids:
        followed = subprocess.run(base + [
            "-q", "-z", f"follow,tls,raw,{stream_id}",
        ], check=False, capture_output=True, timeout=120)
        if len(followed.stdout) > MAX_TLS_FOLLOW_BYTES:
            raise ValueError("tls_diagnostics_failed")
        if followed.returncode:
            stream_failures += 1
            continue
        request, response = _parse_tls_follow_raw(followed.stdout)
        size = len(request) + len(response)
        if not size:
            continue
        plaintext_bytes += size
        if plaintext_bytes > MAX_TLS_FOLLOW_BYTES:
            raise ValueError("tls_diagnostics_failed")
        plaintext_streams[str(stream_id)] = {
            "request": bytearray(request),
            "response": bytearray(response),
        }
    return plaintext_streams, {
        "tls_follow_streams_seen": len(stream_ids),
        "tls_follow_streams_with_plaintext": len(plaintext_streams),
        "tls_follow_plaintext_bytes": plaintext_bytes,
        "tls_follow_http_messages": 0,
        "tls_follow_probe_failures": 0,
        "tls_follow_stream_failures": stream_failures,
    }


def _tls_follow_http1(capture: Path, keys: Path,
                      static: dict[str, str]) -> tuple[
                          list[dict[str, Any]], int, dict[str, int]
                      ]:
    plaintext_streams, diagnostics = _tls_follow_plaintext(capture, keys)
    transactions, messages = _parse_http1_fallback(plaintext_streams, static)
    diagnostics["tls_follow_http_messages"] = messages
    return transactions, messages, diagnostics


def _validate_bundle(bundle: Path, directory: Path) -> tuple[Path, Path]:
    if not zipfile.is_zipfile(bundle):
        raise ValueError("invalid_tls_bundle")
    with zipfile.ZipFile(bundle) as archive:
        infos = archive.infolist()
        if {info.filename for info in infos} != ARCHIVE_MEMBERS or len(infos) != 2:
            raise ValueError("invalid_tls_bundle_members")
        by_name = {info.filename: info for info in infos}
        if not 0 < by_name["capture.pcapng"].file_size <= MAX_CAPTURE_BYTES:
            raise ValueError("capture_size_out_of_range")
        if not 0 < by_name["tls.keys"].file_size <= MAX_KEYS_BYTES:
            raise ValueError("keylog_size_out_of_range")
        capture = directory / "capture.pcapng"
        keys = directory / "tls.keys"
        for name, target in (("capture.pcapng", capture), ("tls.keys", keys)):
            with archive.open(name) as source, target.open("xb") as output:
                total = 0
                while chunk := source.read(1024 * 1024):
                    total += len(chunk)
                    if total > by_name[name].file_size:
                        raise ValueError("archive_member_size_mismatch")
                    output.write(chunk)
                if total != by_name[name].file_size:
                    raise ValueError("archive_member_size_mismatch")
    key_lines = keys.read_text(encoding="ascii", errors="strict").splitlines()
    if not key_lines or any(not KEYLOG_LINE.fullmatch(line.strip()) for line in key_lines if line.strip()):
        raise ValueError("invalid_tls_keylog")
    return capture, keys


def analyze(bundle: Path, static_registry: Path | None = None) -> dict[str, Any]:
    with tempfile.TemporaryDirectory(prefix="aion2-market-tls-") as temporary:
        directory = Path(temporary)
        capture, keys = _validate_bundle(bundle, directory)
        tshark_json = directory / "http.json"
        command = [
            "tshark", "-n", "-r", str(capture),
            "-o", f"tls.keylog_file:{keys}",
            "-o", "tcp.desegment_tcp_streams:TRUE",
            "-o", "tls.desegment_ssl_records:TRUE",
            "-o", "tls.desegment_ssl_application_data:TRUE",
            "-Y", "http or http2 or (tls and data.data)",
            "-T", "json",
        ]
        environment = os.environ.copy()
        environment.pop("SSLKEYLOGFILE", None)
        with tshark_json.open("xb") as output:
            completed = subprocess.run(
                command, check=False, stdout=output, stderr=subprocess.DEVNULL,
                timeout=300, env=environment,
            )
        if completed.returncode or tshark_json.stat().st_size > MAX_TSHARK_JSON_BYTES:
            raise ValueError("market_tls_dissector_failed")
        packets = json.loads(tshark_json.read_text(encoding="utf-8"))
        static = _static_materials(static_registry)
        follow_transactions, follow_messages, follow_diagnostics = (
            _tls_follow_http1(capture, keys, static)
        )
        keylog_diagnostics = _keylog_match_counts(capture, keys)
        late_transactions: list[dict[str, Any]] = []
        late_messages = 0
        late_diagnostics = {
            "late_attach_streams_seen": 0,
            "late_attach_streams_decrypted": 0,
            "late_attach_plaintext_bytes": 0,
            "late_attach_http_messages": 0,
            "late_attach_aead_trials": 0,
        }
        if (not follow_transactions
                and keylog_diagnostics["keylog_application_sessions_available"] > 0
                and keylog_diagnostics["matching_application_sessions"] == 0):
            late_transactions, late_messages, late_diagnostics = (
                _tls13_late_attach_http1(capture, keys, static)
            )
        selected_transactions = follow_transactions or late_transactions
        selected_messages = follow_messages if follow_transactions else late_messages
        diagnostics = {
            **_tls_decode_diagnostics(capture, keys),
            **follow_diagnostics,
            **late_diagnostics,
        }
        return parse_tshark_packets(
            packets,
            static,
            {**keylog_diagnostics, **diagnostics},
            selected_transactions,
            selected_messages,
        )


def validate(report: Any) -> dict[str, Any]:
    if not isinstance(report, dict) or set(report) != {
        "format", "authority", "tls", "transactions", "privacy"
    }:
        raise ValueError("unsafe_market_tls_contract")
    if report["format"] != FORMAT or report["authority"] != AUTHORITY:
        raise ValueError("unsafe_market_tls_contract")
    if set(report["tls"]) != {
        "decrypted_http_messages", "ephemeral_keylog_used", "key_bytes_exported",
        "handshakes_seen", "keylog_sessions_available", "keylog_session_matches",
        "keylog_application_sessions_available", "matching_application_sessions",
        "encrypted_application_frames", "recognized_application_frames",
        "application_protocols", "tcp_gap_frames", "tcp_out_of_order_frames",
        "tls_reassembly_error_frames",
        "tls_follow_streams_seen", "tls_follow_streams_with_plaintext",
        "tls_follow_plaintext_bytes", "tls_follow_http_messages",
        "tls_follow_probe_failures", "tls_follow_stream_failures",
        "late_attach_streams_seen", "late_attach_streams_decrypted",
        "late_attach_plaintext_bytes", "late_attach_http_messages",
        "late_attach_aead_trials",
    } or type(report["tls"]["decrypted_http_messages"]) is not int or report["tls"]["decrypted_http_messages"] < 0 or report["tls"]["ephemeral_keylog_used"] is not True or report["tls"]["key_bytes_exported"] is not False:
        raise ValueError("unsafe_market_tls_contract")
    for field in ("handshakes_seen", "keylog_sessions_available",
                  "keylog_session_matches", "keylog_application_sessions_available",
                  "matching_application_sessions", "encrypted_application_frames",
                  "recognized_application_frames", "tcp_gap_frames",
                  "tcp_out_of_order_frames", "tls_reassembly_error_frames",
                  "tls_follow_streams_seen", "tls_follow_streams_with_plaintext",
                  "tls_follow_plaintext_bytes", "tls_follow_http_messages",
                  "tls_follow_probe_failures", "tls_follow_stream_failures",
                  "late_attach_streams_seen", "late_attach_streams_decrypted",
                  "late_attach_plaintext_bytes", "late_attach_http_messages",
                  "late_attach_aead_trials"):
        if type(report["tls"][field]) is not int or report["tls"][field] < 0:
            raise ValueError("unsafe_market_tls_contract")
    if report["tls"]["tls_follow_streams_with_plaintext"] > report["tls"]["tls_follow_streams_seen"]:
        raise ValueError("unsafe_market_tls_contract")
    if (report["tls"]["late_attach_streams_decrypted"]
            > report["tls"]["late_attach_streams_seen"]
            or report["tls"]["late_attach_streams_seen"] > MAX_LATE_ATTACH_STREAMS
            or report["tls"]["late_attach_plaintext_bytes"] > MAX_TLS_FOLLOW_BYTES
            or report["tls"]["late_attach_aead_trials"] > MAX_LATE_ATTACH_AEAD_TRIALS):
        raise ValueError("unsafe_market_tls_contract")
    if report["tls"]["keylog_session_matches"] > min(
        report["tls"]["handshakes_seen"],
        report["tls"]["keylog_sessions_available"],
    ):
        raise ValueError("unsafe_market_tls_contract")
    if report["tls"]["matching_application_sessions"] > min(
        report["tls"]["handshakes_seen"],
        report["tls"]["keylog_application_sessions_available"],
    ):
        raise ValueError("unsafe_market_tls_contract")
    if (report["tls"]["recognized_application_frames"]
            > report["tls"]["encrypted_application_frames"]
            or not isinstance(report["tls"]["application_protocols"], list)
            or len(report["tls"]["application_protocols"]) > 32
            or any(not isinstance(protocol, str)
                   or not re.fullmatch(r"[a-z0-9_.+-]{1,64}", protocol)
                   for protocol in report["tls"]["application_protocols"])):
        raise ValueError("unsafe_market_tls_contract")
    expected_privacy = {
        "raw_capture_exported": False,
        "raw_body_exported": False,
        "header_values_exported_only_when_safe": True,
        "credentials_exported": False,
        "account_player_seller_values_exported": False,
        "unsafe_fields_exported": False,
    }
    if report["privacy"] != expected_privacy or not isinstance(report["transactions"], list) or len(report["transactions"]) > MAX_TRANSACTIONS:
        raise ValueError("unsafe_market_tls_contract")
    encoded = json.dumps(report, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    if len(encoded.encode("utf-8")) > MAX_REPORT_BYTES:
        raise ValueError("market_tls_report_too_large")
    lowered = encoded.lower()
    for forbidden in ("payload_hex", "authorization: bearer", "set-cookie:", "client_random", "traffic_secret"):
        if forbidden in lowered:
            raise ValueError("unsafe_market_tls_contract")
    return report


def safe_failure_code(error: BaseException) -> str:
    """Collapse private analyzer failures to a fixed non-sensitive vocabulary."""
    if isinstance(error, subprocess.TimeoutExpired):
        return "market_tls_dissector_timeout"
    if isinstance(error, zipfile.BadZipFile):
        return "invalid_tls_bundle"
    if isinstance(error, json.JSONDecodeError):
        return "tshark_output_invalid"
    if isinstance(error, UnicodeError):
        return "invalid_tls_keylog"
    if isinstance(error, ValueError) and str(error) in SAFE_ERROR_CODES:
        return str(error)
    if isinstance(error, OSError):
        return "market_tls_io_failed"
    return "market_tls_analysis_failed"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--static-registry", type=Path)
    args = parser.parse_args()
    try:
        report = analyze(args.input, args.static_registry)
        with args.out.open("x", encoding="utf-8", newline="\n") as stream:
            json.dump(report, stream, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            stream.write("\n")
    except (OSError, UnicodeError, ValueError, zipfile.BadZipFile,
            json.JSONDecodeError, subprocess.SubprocessError) as error:
        print(json.dumps({
            "schema_version": "aion2.market-tls-error.v1",
            "error": safe_failure_code(error),
            "raw_exposed": False,
            "paths_exposed": False,
        }, sort_keys=True), file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
