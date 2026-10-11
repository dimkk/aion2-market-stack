#!/usr/bin/env python3
"""Probe the read-only whole-market minimum-price summary with a live session.

The private analyzer recovers one exact ``sales/valid/summary/min_unit_price``
request from an ordinary client action, keeps the session headers in memory,
and sends one fresh request to the same first-party authority.  Only validated
item IDs, minimum prices, sale counts and item conditions leave the VM.
"""

from __future__ import annotations

import argparse
from decimal import Decimal, InvalidOperation
import json
import os
from pathlib import Path
import re
import tempfile
import time
from typing import Any, Callable
from urllib.parse import parse_qs, urlsplit

import market_session_probe as listing_probe


FORMAT = "aion2_market_summary_probe_v1"
SHAPE_FORMAT = "aion2_market_summary_shape_v1"
MULTI_FORMAT = "aion2_market_multi_summary_probe_v1"
TARGET_BATCH_FORMAT = "aion2_market_summary_target_batch_v1"
AUTHORITY = listing_probe.AUTHORITY
PATH = "/trade/v1.0/sales/valid/summary/min_unit_price"
METHOD = "POST"
MAX_SUMMARIES = 10_000
MAX_REPORT_BYTES = 4 * 1024 * 1024
MAX_MULTI_REPORT_BYTES = 32 * 1024 * 1024
MAX_SERVER_TARGETS = 100
MAX_TRANSPORT_ATTEMPTS_PER_SCOPE = 3
MIN_INTERVAL_SECONDS = 1.0
HEX_64 = re.compile(r"^[0-9a-f]{64}$")
SAFE_ERRORS = listing_probe.SAFE_ERRORS | frozenset({
    "market_summary_target_batch_invalid",
    "market_multi_summary_report_too_large",
    "market_session_rate_limited",
    "market_session_upstream_unavailable",
})
SAFE_TEMPLATE_SOURCE_PATHS = frozenset({
    PATH,
    listing_probe.PATH,
    "/trade/v1.0/policies",
    "/trade/v1.0/me/earnings",
    "/trade/v1.0/me/earnings/changes",
    "/trade/v1.0/me/favorites",
    "/trade/v1.0/me/sales",
    "/trade/v1.0/sales/summary/higher/min_unit_price",
})


def _request_body(value: bytes) -> dict[str, int]:
    try:
        body = json.loads(value.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError("market_session_request_invalid") from error
    if not isinstance(body, dict) or set(body) != {"game_server_id"}:
        raise ValueError("market_session_request_invalid")
    return {
        "game_server_id": listing_probe._integer(
            body["game_server_id"], 1, 0xFFFFFFFF
        )
    }


def _summary_requests(streams: dict[str, dict[str, bytearray]]) -> list[dict[str, Any]]:
    candidates: list[tuple[int, int, dict[str, Any]]] = []
    for stream, directions in streams.items():
        try:
            stream_order = int(stream)
        except ValueError:
            continue
        messages = listing_probe.tls_contract._http1_messages(
            bytes(directions["request"]), True
        )
        for index, message in enumerate(messages):
            if message["first_line"] != f"{METHOD} {PATH} HTTP/1.1":
                continue
            if message["headers"].get("host", "").lower().rstrip(".") != AUTHORITY:
                continue
            candidates.append((stream_order, index, message))
    candidates.sort(key=lambda row: (row[0], row[1]))
    return [row[2] for row in candidates]


def _market_requests(
    streams: dict[str, dict[str, bytearray]],
) -> list[tuple[str, dict[str, Any], int]]:
    """Return bounded authenticated-market template sources in wire order.

    The overview request is not guaranteed to be emitted on every market open:
    the client can satisfy it from its own cache.  Other observed read-only
    market requests to the same first-party authority still carry the exact
    current session headers and server scope needed for the bounded summary
    POST.  Mutation and unknown paths are never accepted as template sources.
    """
    candidates: list[tuple[int, int, str, dict[str, Any], int]] = []
    for stream, directions in streams.items():
        try:
            stream_order = int(stream)
        except ValueError:
            continue
        messages = listing_probe.tls_contract._http1_messages(
            bytes(directions["request"]), True
        )
        for index, message in enumerate(messages):
            parts = message["first_line"].split(" ")
            if len(parts) != 3 or parts[2] != "HTTP/1.1":
                continue
            request_method, target, _version = parts
            if request_method not in {"GET", "POST"}:
                continue
            parsed_target = urlsplit(target)
            path = parsed_target.path
            if path not in SAFE_TEMPLATE_SOURCE_PATHS:
                continue
            if message["headers"].get("host", "").lower().rstrip(".") != AUTHORITY:
                continue
            try:
                if path == PATH:
                    game_server_id = _request_body(message["body"])["game_server_id"]
                elif path == listing_probe.PATH:
                    game_server_id = listing_probe._integer(
                        listing_probe._request_body(message["body"])["game_server_id"],
                        1, 0xFFFFFFFF,
                    )
                else:
                    query_values = parse_qs(
                        parsed_target.query, keep_blank_values=True
                    ).get("game_server_id", [])
                    if len(query_values) == 1:
                        game_server_id = listing_probe._integer(
                            query_values[0], 1, 0xFFFFFFFF
                        )
                    else:
                        decoded = json.loads(message["body"].decode("utf-8"))
                        if not isinstance(decoded, dict) or "game_server_id" not in decoded:
                            continue
                        game_server_id = listing_probe._integer(
                            decoded["game_server_id"], 1, 0xFFFFFFFF
                        )
            except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
                continue
            candidates.append(
                (stream_order, index, path, message, game_server_id)
            )
    candidates.sort(key=lambda row: (row[0], row[1]))
    return [(row[2], row[3], row[4]) for row in candidates]


def _select_template(streams: dict[str, dict[str, bytearray]]) -> dict[str, Any]:
    candidates = _market_requests(streams)
    if not candidates:
        raise ValueError("market_session_template_not_found")
    for _path, message, game_server_id in reversed(candidates):
        headers = message["headers"]
        authorization = headers.get("authorization", "")
        if (not listing_probe.JWT.fullmatch(authorization)
                or not headers.get("client-context")
                or not headers.get("client-game-context")):
            continue
        body = {"game_server_id": game_server_id}
        private_headers = {
            name: value for name, value in headers.items()
            if name in listing_probe.CLONED_SESSION_HEADERS
            and isinstance(value, str) and value
        }
        private_headers.update({
            "Accept": "application/json",
            "Accept-Encoding": "identity",
            "Content-Type": "application/json",
            "Connection": "close",
        })
        return {"headers": private_headers, "body": body}
    raise ValueError("market_session_auth_missing")


def _extract_template(bundle: Path) -> dict[str, Any]:
    with tempfile.TemporaryDirectory(prefix="aion2-market-summary-") as temporary:
        capture, keys = listing_probe.tls_contract._validate_bundle(
            bundle, Path(temporary)
        )
        keylog_match = listing_probe.tls_contract._keylog_match_counts(capture, keys)
        if keylog_match["matching_application_sessions"]:
            streams, _diagnostics = listing_probe.tls_contract._tls_follow_plaintext(
                capture, keys
            )
            if _market_requests(streams):
                return _select_template(streams)
        # Stop at the first scoped market request rather than waiting for a
        # particular sales POST.  Reused TLS sessions commonly contain an
        # earlier policies GET with ``game_server_id`` in its query string.
        streams, _diagnostics = (
            listing_probe.tls_contract._tls13_late_attach_plaintext(
                capture, keys, b"game_server_id"
            )
        )
        return _select_template(streams)


def _https_post(headers: dict[str, str], body: bytes) -> tuple[int, dict[str, str], bytes]:
    if os.environ.get("AION2_FIXED_TLS_RELAY") != "127.0.0.1:41443":
        raise ValueError("market_session_relay_missing")
    connection = listing_probe._FixedRelayHTTPSConnection(
        AUTHORITY, 443, timeout=20,
        context=listing_probe.ssl.create_default_context(),
    )
    try:
        connection.request(METHOD, PATH, body=body, headers=headers)
        response = connection.getresponse()
        raw = response.read(listing_probe.MAX_RESPONSE_BYTES + 1)
        if len(raw) > listing_probe.MAX_RESPONSE_BYTES:
            raise ValueError("market_session_response_too_large")
        response_headers = {
            name.lower(): value for name, value in response.getheaders()
        }
        return response.status, response_headers, listing_probe._decode_response(
            raw, response_headers.get("content-encoding", "")
        )
    except ValueError:
        raise
    except (OSError, listing_probe.http.client.HTTPException,
            listing_probe.ssl.SSLError) as error:
        raise ValueError("market_session_network_failed") from error
    finally:
        connection.close()


def _decimal(value: Any) -> str:
    if not isinstance(value, (str, int, float)) or isinstance(value, bool):
        raise ValueError("market_session_response_invalid")
    try:
        number = Decimal(str(value))
    except InvalidOperation as error:
        raise ValueError("market_session_response_invalid") from error
    if not number.is_finite() or number < 0 or number > Decimal("999999999999999"):
        raise ValueError("market_session_response_invalid")
    return format(number, "f")


def _summary(value: Any) -> dict[str, Any]:
    required = {"game_item_key", "min_unit_price", "sale_count"}
    if not isinstance(value, dict) or not required.issubset(value):
        raise ValueError("market_session_response_invalid")
    return {
        "game_item_id": listing_probe._integer(
            value["game_item_key"], 1, 0xFFFFFFFF
        ),
        "min_unit_price": _decimal(value["min_unit_price"]),
        "sale_count": listing_probe._integer(
            value["sale_count"], 0, 0xFFFFFFFF
        ),
        "conditions": listing_probe._conditions(
            value.get("game_item_conditions", [])
        ),
    }


def _summary_rows(root: Any) -> list[Any]:
    """Select one unambiguous list whose rows satisfy the summary contract.

    The observed API has used wrapper names other than ``list`` across client
    cohorts.  The field name is not semantically important: every exported row
    still has to pass the exact item/price/count/conditions validator below.
    Nested traversal is deliberately limited to one wrapper level.
    """
    if isinstance(root, list):
        return root
    if not isinstance(root, dict):
        raise ValueError("market_session_response_invalid")
    required = {"game_item_key", "min_unit_price", "sale_count"}
    if required.issubset(root):
        return [root]
    candidates: list[list[Any]] = []
    values = list(root.items())
    values.extend(
        (nested_name, nested)
        for value in root.values() if isinstance(value, dict)
        for nested_name, nested in value.items()
    )
    empty_wrapper_names = {
        "list", "minimum_prices", "min_unit_prices", "summaries"
    }
    for name, value in values:
        if not isinstance(value, list):
            continue
        if not value and name not in empty_wrapper_names:
            continue
        if value and not all(
            isinstance(row, dict) and required.issubset(row) for row in value
        ):
            continue
        if all(value is not candidate for candidate in candidates):
            candidates.append(value)
    if len(candidates) != 1:
        raise ValueError("market_session_response_invalid")
    return candidates[0]


def _normalize_response(
    status: int, headers: dict[str, str], data: bytes
) -> dict[str, Any]:
    if status in (401, 403):
        raise listing_probe.MarketSessionAuthRejected(status)
    if status == 429:
        raise ValueError("market_session_rate_limited")
    if 500 <= status <= 599:
        raise ValueError("market_session_upstream_unavailable")
    if status != 200:
        raise ValueError("market_session_http_rejected")
    content_type = headers.get("content-type", "").split(";", 1)[0].strip().lower()
    if content_type not in ("application/json", "text/json", ""):
        raise ValueError("market_session_response_not_json")
    try:
        root = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError("market_session_response_not_json") from error
    rows = _summary_rows(root)
    if len(rows) > MAX_SUMMARIES:
        raise ValueError("market_session_response_invalid")
    summaries = [_summary(row) for row in rows]
    return {
        "status_code": status,
        "summary_count": len(summaries),
        "summaries": summaries,
    }


def _safe_response_shape(
    status: int, headers: dict[str, str], data: bytes
) -> dict[str, Any]:
    """Return schema-only response evidence; never response values or headers."""
    try:
        root = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError("market_session_response_not_json") from error
    name_pattern = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,63}$")
    forbidden = {
        "authorization", "access_token", "cookie", "client_context",
        "client_game_context", "device_tag", "seller", "account", "player",
    }

    def names(value: Any) -> list[str]:
        if not isinstance(value, dict):
            return []
        result = sorted(value)
        if (len(result) > 64 or any(
                not isinstance(name, str) or not name_pattern.fullmatch(name)
                or name.lower() in forbidden for name in result)):
            raise ValueError("market_session_response_invalid")
        return result

    arrays: list[dict[str, Any]] = []
    if isinstance(root, list):
        arrays.append({
            "path": "$",
            "count": len(root),
            "sample_fields": names(root[0]) if root else [],
        })
        top_level_fields: list[str] = []
        root_type = "array"
    elif isinstance(root, dict):
        top_level_fields = names(root)
        root_type = "object"
        for name, value in root.items():
            if isinstance(value, list):
                arrays.append({
                    "path": name,
                    "count": len(value),
                    "sample_fields": names(value[0]) if value else [],
                })
            elif isinstance(value, dict):
                for nested_name, nested in value.items():
                    if isinstance(nested, list):
                        arrays.append({
                            "path": f"{name}.{nested_name}",
                            "count": len(nested),
                            "sample_fields": names(nested[0]) if nested else [],
                        })
    else:
        top_level_fields = []
        root_type = type(root).__name__
    if len(arrays) > 32:
        raise ValueError("market_session_response_invalid")
    return {
        "status_code": status,
        "content_type": headers.get("content-type", "").split(";", 1)[0].lower(),
        "body_bytes": len(data),
        "root_type": root_type,
        "top_level_fields": top_level_fields,
        "arrays": arrays,
    }


def execute_probe(
    template: dict[str, Any],
    sender: Callable[
        [dict[str, str], bytes], tuple[int, dict[str, str], bytes]
    ] = _https_post,
) -> dict[str, Any]:
    body = json.dumps(
        template["body"], ensure_ascii=False, separators=(",", ":")
    ).encode("utf-8")
    try:
        status, response_headers, response_body = sender(template["headers"], body)
        try:
            normalized = _normalize_response(
                status, response_headers, response_body
            )
            report_format = FORMAT
            response_name = "response"
        except ValueError as error:
            if str(error) != "market_session_response_invalid":
                raise
            normalized = _safe_response_shape(
                status, response_headers, response_body
            )
            report_format = SHAPE_FORMAT
            response_name = "response_shape"
        report = {
            "format": report_format,
            "authority": AUTHORITY,
            "method": METHOD,
            "path": PATH,
            "request": dict(template["body"]),
            response_name: normalized,
            "auth": {
                "kind": "client_session_jwt_memory_only",
                "client_must_remain_open": True,
                "persisted": False,
                "exported": False,
            },
            "privacy": {
                "fresh_https_request": True,
                "max_requests": 1,
                "credentials_exported": False,
                "raw_headers_exported": False,
                "raw_bodies_exported": False,
                "paths_exposed": False,
            },
        }
        return validate(report)
    finally:
        template.get("headers", {}).clear()


def load_target_batch(path: Path) -> tuple[list[int], str, str]:
    raw = path.read_bytes()
    if len(raw) > 256 * 1024:
        raise ValueError("market_summary_target_batch_invalid")
    document = json.loads(raw.decode("utf-8"))
    if (
        not isinstance(document, dict)
        or set(document) != {
            "format", "generated_at", "registry_sha256", "game_server_ids"
        }
        or document.get("format") != TARGET_BATCH_FORMAT
        or not isinstance(document.get("generated_at"), str)
        or not HEX_64.fullmatch(str(document.get("registry_sha256", "")))
        or not isinstance(document.get("game_server_ids"), list)
    ):
        raise ValueError("market_summary_target_batch_invalid")
    server_ids = document["game_server_ids"]
    if (
        not 1 <= len(server_ids) <= MAX_SERVER_TARGETS
        or any(type(value) is not int or not 1 <= value <= 0xFFFFFFFF
               for value in server_ids)
        or len(set(server_ids)) != len(server_ids)
    ):
        raise ValueError("market_summary_target_batch_invalid")
    return server_ids, __import__("hashlib").sha256(raw).hexdigest(), document["registry_sha256"]


def execute_multi_probe(
    template: dict[str, Any],
    server_ids: list[int],
    target_batch_sha256: str,
    registry_sha256: str,
    sender: Callable[
        [dict[str, str], bytes], tuple[int, dict[str, str], bytes]
    ] = _https_post,
    sleeper: Callable[[float], None] = time.sleep,
    transport_attempt_counter: Callable[[], int] | None = None,
) -> dict[str, Any]:
    if (
        not 1 <= len(server_ids) <= MAX_SERVER_TARGETS
        or len(set(server_ids)) != len(server_ids)
        or any(type(value) is not int or not 1 <= value <= 0xFFFFFFFF
               for value in server_ids)
        or not HEX_64.fullmatch(target_batch_sha256)
        or not HEX_64.fullmatch(registry_sha256)
    ):
        raise ValueError("market_summary_target_batch_invalid")
    responses: list[dict[str, Any]] = []
    cumulative_response_bytes = 0
    try:
        for index, game_server_id in enumerate(server_ids):
            if index:
                sleeper(MIN_INTERVAL_SECONDS)
            body = json.dumps(
                {"game_server_id": game_server_id}, separators=(",", ":")
            ).encode("utf-8")
            status, response_headers, response_body = sender(
                template["headers"], body
            )
            normalized = _normalize_response(
                status, response_headers, response_body
            )
            response_record = {
                "game_server_id": game_server_id,
                **normalized,
            }
            cumulative_response_bytes += len(json.dumps(
                response_record,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8"))
            if cumulative_response_bytes > MAX_MULTI_REPORT_BYTES:
                raise ValueError("market_multi_summary_report_too_large")
            responses.append(response_record)
        report = {
            "format": MULTI_FORMAT,
            "authority": AUTHORITY,
            "method": METHOD,
            "path": PATH,
            "request": {
                "game_server_ids": server_ids,
                "minimum_interval_seconds": MIN_INTERVAL_SECONDS,
            },
            "responses": responses,
            "transport_attempts": (
                len(server_ids)
                if transport_attempt_counter is None
                else transport_attempt_counter()
            ),
            "target_batch_sha256": target_batch_sha256,
            "registry_sha256": registry_sha256,
            "catalog_sha256": target_batch_sha256,
            "auth": {
                "kind": "client_session_jwt_memory_only",
                "client_must_remain_open": True,
                "persisted": False,
                "exported": False,
            },
            "privacy": {
                "fresh_https_request": True,
                "max_requests": len(server_ids) * MAX_TRANSPORT_ATTEMPTS_PER_SCOPE,
                "credentials_exported": False,
                "raw_headers_exported": False,
                "raw_bodies_exported": False,
                "paths_exposed": False,
            },
        }
        return validate(report)
    finally:
        template.get("headers", {}).clear()


def validate(report: Any) -> dict[str, Any]:
    if isinstance(report, dict) and report.get("format") == MULTI_FORMAT:
        expected = {
            "format", "authority", "method", "path", "request", "responses",
            "target_batch_sha256", "registry_sha256", "catalog_sha256",
            "transport_attempts", "auth", "privacy",
        }
        if set(report) != expected:
            raise ValueError("market_session_response_invalid")
        request = report["request"]
        server_ids = request.get("game_server_ids") if isinstance(request, dict) else None
        if (
            report["authority"] != AUTHORITY
            or report["method"] != METHOD
            or report["path"] != PATH
            or set(request) != {"game_server_ids", "minimum_interval_seconds"}
            or not isinstance(server_ids, list)
            or not 1 <= len(server_ids) <= MAX_SERVER_TARGETS
            or len(set(server_ids)) != len(server_ids)
            or any(type(value) is not int or not 1 <= value <= 0xFFFFFFFF
                   for value in server_ids)
            or request["minimum_interval_seconds"] != MIN_INTERVAL_SECONDS
            or not HEX_64.fullmatch(str(report["target_batch_sha256"]))
            or not HEX_64.fullmatch(str(report["registry_sha256"]))
            or report["catalog_sha256"] != report["target_batch_sha256"]
            or not isinstance(report["responses"], list)
            or len(report["responses"]) != len(server_ids)
            or type(report["transport_attempts"]) is not int
            or not len(server_ids) <= report["transport_attempts"]
                <= len(server_ids) * MAX_TRANSPORT_ATTEMPTS_PER_SCOPE
        ):
            raise ValueError("market_session_response_invalid")
        for expected_server_id, response in zip(server_ids, report["responses"]):
            if (
                not isinstance(response, dict)
                or set(response) != {
                    "game_server_id", "status_code", "summary_count", "summaries"
                }
                or response["game_server_id"] != expected_server_id
            ):
                raise ValueError("market_session_response_invalid")
            validate({
                "format": FORMAT,
                "authority": AUTHORITY,
                "method": METHOD,
                "path": PATH,
                "request": {"game_server_id": expected_server_id},
                "response": {
                    "status_code": response["status_code"],
                    "summary_count": response["summary_count"],
                    "summaries": response["summaries"],
                },
                "auth": report["auth"],
                "privacy": {
                    "fresh_https_request": True,
                    "max_requests": 1,
                    "credentials_exported": False,
                    "raw_headers_exported": False,
                    "raw_bodies_exported": False,
                    "paths_exposed": False,
                },
            })
        if report["auth"] != {
            "kind": "client_session_jwt_memory_only",
            "client_must_remain_open": True,
            "persisted": False,
            "exported": False,
        } or report["privacy"] != {
            "fresh_https_request": True,
            "max_requests": len(server_ids) * MAX_TRANSPORT_ATTEMPTS_PER_SCOPE,
            "credentials_exported": False,
            "raw_headers_exported": False,
            "raw_bodies_exported": False,
            "paths_exposed": False,
        }:
            raise ValueError("market_session_response_invalid")
        encoded = json.dumps(
            report, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
        if len(encoded.encode("utf-8")) > MAX_MULTI_REPORT_BYTES:
            raise ValueError("market_multi_summary_report_too_large")
        lowered = encoded.lower()
        if any(forbidden in lowered for forbidden in (
            "authorization", "client-context", "client-game-context", "device-tag",
            "access_token", "cookie", "seller", "account", "player",
        )):
            raise ValueError("market_session_response_invalid")
        return report
    if isinstance(report, dict) and report.get("format") == SHAPE_FORMAT:
        expected = {
            "format", "authority", "method", "path", "request",
            "response_shape", "auth", "privacy",
        }
        if set(report) != expected:
            raise ValueError("market_session_response_invalid")
        shape = report["response_shape"]
        field_name = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,63}$")
        array_path = re.compile(
            r"^(?:\$|[A-Za-z][A-Za-z0-9_]{0,63}(?:\.[A-Za-z][A-Za-z0-9_]{0,63})?)$"
        )
        if (report["authority"] != AUTHORITY or report["method"] != METHOD
                or report["path"] != PATH
                or not isinstance(report["request"], dict)
                or set(report["request"]) != {"game_server_id"}
                or type(report["request"]["game_server_id"]) is not int
                or not isinstance(shape, dict)
                or set(shape) != {
                    "status_code", "content_type", "body_bytes", "root_type",
                    "top_level_fields", "arrays",
                }
                or shape["status_code"] != 200
                or shape["content_type"] not in {"application/json", "text/json", ""}
                or type(shape["body_bytes"]) is not int
                or not 0 <= shape["body_bytes"] <= listing_probe.MAX_RESPONSE_BYTES
                or shape["root_type"] not in {"object", "array", "str", "int", "float", "bool", "NoneType"}
                or not isinstance(shape["top_level_fields"], list)
                or not isinstance(shape["arrays"], list)):
            raise ValueError("market_session_response_invalid")
        if (len(shape["top_level_fields"]) > 64
                or any(not isinstance(name, str) or not field_name.fullmatch(name)
                       for name in shape["top_level_fields"])
                or len(shape["arrays"]) > 32):
            raise ValueError("market_session_response_invalid")
        for row in shape["arrays"]:
            if (not isinstance(row, dict)
                    or set(row) != {"path", "count", "sample_fields"}
                    or not isinstance(row["path"], str)
                    or not array_path.fullmatch(row["path"])
                    or type(row["count"]) is not int
                    or not 0 <= row["count"] <= MAX_SUMMARIES
                    or not isinstance(row["sample_fields"], list)
                    or len(row["sample_fields"]) > 64
                    or any(not isinstance(name, str) or not field_name.fullmatch(name)
                           for name in row["sample_fields"])):
                raise ValueError("market_session_response_invalid")
        if report["auth"] != {
            "kind": "client_session_jwt_memory_only",
            "client_must_remain_open": True,
            "persisted": False,
            "exported": False,
        } or report["privacy"] != {
            "fresh_https_request": True,
            "max_requests": 1,
            "credentials_exported": False,
            "raw_headers_exported": False,
            "raw_bodies_exported": False,
            "paths_exposed": False,
        }:
            raise ValueError("market_session_response_invalid")
        encoded = json.dumps(report, ensure_ascii=False, sort_keys=True)
        lowered = encoded.lower()
        if len(encoded.encode("utf-8")) > MAX_REPORT_BYTES or any(
            forbidden in lowered for forbidden in (
                "authorization", "access_token", "cookie", "client-context",
                "client-game-context", "device-tag", "seller", "account", "player",
            )
        ):
            raise ValueError("market_session_response_invalid")
        return report
    if not isinstance(report, dict) or set(report) != {
        "format", "authority", "method", "path", "request", "response",
        "auth", "privacy",
    }:
        raise ValueError("market_session_response_invalid")
    if (report["format"] != FORMAT or report["authority"] != AUTHORITY
            or report["method"] != METHOD or report["path"] != PATH):
        raise ValueError("market_session_response_invalid")
    request, response = report["request"], report["response"]
    if (not isinstance(request, dict) or set(request) != {"game_server_id"}
            or type(request["game_server_id"]) is not int
            or not 1 <= request["game_server_id"] <= 0xFFFFFFFF
            or not isinstance(response, dict)
            or set(response) != {"status_code", "summary_count", "summaries"}
            or response["status_code"] != 200
            or type(response["summary_count"]) is not int
            or not isinstance(response["summaries"], list)
            or response["summary_count"] != len(response["summaries"])
            or response["summary_count"] > MAX_SUMMARIES):
        raise ValueError("market_session_response_invalid")
    for row in response["summaries"]:
        if set(row) != {"game_item_id", "min_unit_price", "sale_count", "conditions"}:
            raise ValueError("market_session_response_invalid")
        _summary({
            "game_item_key": row["game_item_id"],
            "min_unit_price": row["min_unit_price"],
            "sale_count": row["sale_count"],
            "game_item_conditions": row["conditions"],
        })
    if report["auth"] != {
        "kind": "client_session_jwt_memory_only",
        "client_must_remain_open": True,
        "persisted": False,
        "exported": False,
    } or report["privacy"] != {
        "fresh_https_request": True,
        "max_requests": 1,
        "credentials_exported": False,
        "raw_headers_exported": False,
        "raw_bodies_exported": False,
        "paths_exposed": False,
    }:
        raise ValueError("market_session_response_invalid")
    encoded = json.dumps(
        report, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    if len(encoded.encode("utf-8")) > MAX_REPORT_BYTES:
        raise ValueError("market_session_report_too_large")
    lowered = encoded.lower()
    for forbidden in (
        "authorization", "client-context", "client-game-context", "device-tag",
        "access_token", "cookie", "seller", "account", "player",
    ):
        if forbidden in lowered:
            raise ValueError("market_session_response_invalid")
    return report


def analyze(bundle: Path, target_batch: Path | None = None) -> dict[str, Any]:
    template = _extract_template(bundle)
    if target_batch is None:
        return execute_probe(template)
    server_ids, batch_sha256, registry_sha256 = load_target_batch(target_batch)
    return execute_multi_probe(
        template, server_ids, batch_sha256, registry_sha256
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--target-batch", type=Path)
    args = parser.parse_args()
    try:
        report = analyze(args.input, args.target_batch)
        if args.out.exists() or args.out.is_symlink():
            raise ValueError("market_session_request_invalid")
        with args.out.open("x", encoding="utf-8", newline="\n") as stream:
            json.dump(
                report, stream, ensure_ascii=False, sort_keys=True,
                separators=(",", ":")
            )
            stream.write("\n")
        return 0
    except (OSError, ValueError, json.JSONDecodeError) as error:
        code = str(error) if str(error) in SAFE_ERRORS else "market_session_network_failed"
        print(json.dumps({
            "schema_version": "aion2.market-summary-probe-error.v1",
            "error": code,
            "credentials_exported": False,
            "raw_exposed": False,
            "paths_exposed": False,
        }), file=__import__("sys").stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
