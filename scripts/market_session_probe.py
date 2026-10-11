#!/usr/bin/env python3
"""Perform one fresh read-only market request from a live client session.

The input bundle is consumed only inside the private analyzer VM.  A current
``Authorization: jwt`` value and the SDK context headers are recovered from an
ordinary client request, kept in process memory, and used for exactly one new
HTTPS POST to the already confirmed listings endpoint.  Neither credentials,
raw headers, raw request/response bodies nor filesystem paths are serialized.

This is deliberately a one-hop acceptance probe, not a crawler.  Parameterized
pagination and item selection are enabled only after this fresh request/response
path has been proven against a live session.
"""

from __future__ import annotations

import argparse
import datetime as dt
import gzip
import http.client
import json
import os
from pathlib import Path
import re
import socket
import ssl
import tempfile
from typing import Any, Callable
import zlib

import market_tls_contract_sanitizer as tls_contract


FORMAT = "aion2_market_session_probe_v1"
AUTHORITY = "qos-api.g.nc.com"
PATH = "/trade/v1.0/sales/valid/pages"
METHOD = "POST"
MAX_RESPONSE_BYTES = 8 * 1024 * 1024
MAX_REPORT_BYTES = 4 * 1024 * 1024
MAX_LISTINGS = 1000
UUID = re.compile(
    r"^[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[1-5][0-9A-Fa-f]{3}-"
    r"[89ABab][0-9A-Fa-f]{3}-[0-9A-Fa-f]{12}$"
)
JWT = re.compile(
    r"^jwt[ \t]+(?=.{20,16384}$)"
    r"(?:[A-Za-z0-9_-]+={0,2}\.){2}[A-Za-z0-9_-]+={0,2}$",
    re.IGNORECASE,
)
NUMERIC_TEXT = re.compile(r"^[0-9]{1,20}$")
DECIMAL_TEXT = re.compile(r"^[0-9]{1,20}(?:\.[0-9]{1,8})?$")
SAFE_CONDITION = re.compile(r"^[A-Za-z0-9_.:+\-/]{1,160}$")
CLONED_SESSION_HEADERS = frozenset({
    "authorization", "client-context", "client-game-context", "device-tag",
    "user-agent", "accept-language",
})
SAFE_ERRORS = frozenset({
    "market_session_template_not_found",
    "market_session_template_ambiguous",
    "market_session_request_invalid",
    "market_session_auth_missing",
    "market_session_network_failed",
    "market_session_relay_missing",
    "market_session_auth_rejected",
    "market_session_http_rejected",
    "market_session_response_too_large",
    "market_session_response_not_json",
    "market_session_response_invalid",
    "market_session_report_too_large",
    "market_session_probe_timeout",
})


class MarketSessionAuthRejected(ValueError):
    """Safe first-party auth rejection with the exact HTTP status."""

    def __init__(self, status: int) -> None:
        if status not in (401, 403):
            raise ValueError("market_session_response_invalid")
        super().__init__("market_session_auth_rejected")
        self.status = status


def _timestamp(value: Any) -> str:
    if not isinstance(value, str) or not 8 <= len(value) <= 48:
        raise ValueError("market_session_response_invalid")
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise ValueError("market_session_response_invalid") from error
    if parsed.tzinfo is None:
        raise ValueError("market_session_response_invalid")
    return value


def _integer(value: Any, low: int, high: int) -> int:
    if isinstance(value, int) and not isinstance(value, bool):
        result = value
    elif isinstance(value, str) and NUMERIC_TEXT.fullmatch(value):
        result = int(value)
    else:
        raise ValueError("market_session_response_invalid")
    if not low <= result <= high:
        raise ValueError("market_session_response_invalid")
    return result


def _request_body(value: bytes) -> dict[str, Any]:
    try:
        body = json.loads(value.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError("market_session_request_invalid") from error
    allowed = {"game_server_id", "game_item_keys", "page_no", "page_size", "sort", "search"}
    if not isinstance(body, dict) or not set(body).issubset(allowed) or not {
        "game_server_id", "game_item_keys", "page_no", "page_size", "sort"
    }.issubset(body):
        raise ValueError("market_session_request_invalid")
    if (not isinstance(body["game_server_id"], str)
            or not NUMERIC_TEXT.fullmatch(body["game_server_id"])
            or not isinstance(body["game_item_keys"], str)
            or not re.fullmatch(r"[0-9]{1,10}(?:,[0-9]{1,10}){0,99}", body["game_item_keys"])
            or type(body["page_no"]) is not int or not 1 <= body["page_no"] <= 10000
            or type(body["page_size"]) is not int or not 1 <= body["page_size"] <= 100):
        raise ValueError("market_session_request_invalid")
    sort = body["sort"]
    if (not isinstance(sort, dict) or set(sort) != {"key", "order_by"}
            or sort["key"] not in tls_contract.SAFE_MARKET_SORT_KEYS
            or sort["order_by"] not in tls_contract.SAFE_MARKET_SORT_ORDERS):
        raise ValueError("market_session_request_invalid")
    if "search" in body:
        search = body["search"]
        if not isinstance(search, list) or len(search) > 16:
            raise ValueError("market_session_request_invalid")
        for row in search:
            if (not isinstance(row, dict) or set(row) != {"key", "value"}
                    or not isinstance(row["key"], str) or not 1 <= len(row["key"]) <= 64
                    or not isinstance(row["value"], str) or not 1 <= len(row["value"]) <= 160
                    or tls_contract.DENY_VALUE_KEY.search(row["key"])):
                raise ValueError("market_session_request_invalid")
    return body


def _listing_requests(streams: dict[str, dict[str, bytearray]]) -> list[dict[str, Any]]:
    candidates: list[tuple[int, int, dict[str, Any]]] = []
    for stream, directions in streams.items():
        try:
            stream_order = int(stream)
        except ValueError:
            continue
        messages = tls_contract._http1_messages(bytes(directions["request"]), True)
        for index, message in enumerate(messages):
            if message["first_line"] != f"{METHOD} {PATH} HTTP/1.1":
                continue
            if message["headers"].get("host", "").lower().rstrip(".") != AUTHORITY:
                continue
            candidates.append((stream_order, index, message))
    candidates.sort(key=lambda row: (row[0], row[1]))
    return [row[2] for row in candidates]


def _select_template(streams: dict[str, dict[str, bytearray]]) -> dict[str, Any]:
    candidates = _listing_requests(streams)
    if not candidates:
        raise ValueError("market_session_template_not_found")
    # A bounded capture can end with a second, partial SDK request to the same
    # listing path.  Do not let that unauthenticated tail shadow an earlier
    # complete request from the same capture.  Walk newest-to-oldest and clone
    # only the first request that carries the full market-session context.
    for message in reversed(candidates):
        headers = message["headers"]
        authorization = headers.get("authorization", "")
        if (not JWT.fullmatch(authorization)
                or not headers.get("client-context")
                or not headers.get("client-game-context")):
            continue
        body = _request_body(message["body"])
        private_headers = {
            name: value for name, value in headers.items()
            if name in CLONED_SESSION_HEADERS and isinstance(value, str) and value
        }
        private_headers.update({
            "Accept": "application/json",
            "Accept-Encoding": "identity",
            "Content-Type": "application/json",
            "Connection": "close",
        })
        return {"headers": private_headers, "body": body}
    raise ValueError("market_session_auth_missing")


def _select_scannable_template(
    streams: dict[str, dict[str, bytearray]],
) -> dict[str, Any]:
    """Prefer an exact listing request, otherwise synthesize its proven shape.

    Any allowlisted authenticated read-only market request can provide the
    current session headers and server scope.  Detail scans overwrite the item
    key/page while preserving this fixed listing contract.
    """
    if _listing_requests(streams):
        return _select_template(streams)
    try:
        import market_summary_probe as summary_probe
    except ModuleNotFoundError:  # repository-side unit tests
        from scripts import market_summary_probe as summary_probe
    source = summary_probe._select_template(streams)
    game_server_id = _integer(source["body"]["game_server_id"], 1, 0xFFFFFFFF)
    return {
        "headers": source["headers"],
        "body": {
            "game_server_id": str(game_server_id),
            "game_item_keys": "516150001",
            "page_no": 1,
            "page_size": 100,
            "sort": {"key": "SalePrice", "order_by": "ASC"},
        },
    }


def _extract_template(bundle: Path) -> dict[str, Any]:
    with tempfile.TemporaryDirectory(prefix="aion2-market-session-") as temporary:
        capture, keys = tls_contract._validate_bundle(bundle, Path(temporary))
        keylog_match = tls_contract._keylog_match_counts(capture, keys)
        if keylog_match["matching_application_sessions"]:
            streams, _diagnostics = tls_contract._tls_follow_plaintext(capture, keys)
            try:
                return _select_scannable_template(streams)
            except ValueError as error:
                if str(error) not in {
                    "market_session_template_not_found", "market_session_auth_missing"
                }:
                    raise
        streams, _diagnostics = tls_contract._tls13_late_attach_plaintext(
            capture, keys, b"game_server_id"
        )
        return _select_scannable_template(streams)


def _decode_response(data: bytes, encoding: str) -> bytes:
    lowered = encoding.lower().strip()
    try:
        if lowered in ("", "identity"):
            return data
        if lowered == "gzip":
            return gzip.decompress(data)
        if lowered == "deflate":
            return zlib.decompress(data)
    except (OSError, zlib.error) as error:
        raise ValueError("market_session_response_invalid") from error
    raise ValueError("market_session_response_invalid")


class _FixedRelayHTTPSConnection(http.client.HTTPSConnection):
    """TLS to AUTHORITY over the exact one-shot SSH relay socket."""

    def connect(self) -> None:
        raw = socket.create_connection(("127.0.0.1", 41443), self.timeout)
        self.sock = self._context.wrap_socket(raw, server_hostname=AUTHORITY)


def _https_post(headers: dict[str, str], body: bytes) -> tuple[int, dict[str, str], bytes]:
    if os.environ.get("AION2_FIXED_TLS_RELAY") != "127.0.0.1:41443":
        raise ValueError("market_session_relay_missing")
    connection = _FixedRelayHTTPSConnection(
        AUTHORITY, 443, timeout=20, context=ssl.create_default_context()
    )
    try:
        connection.request(METHOD, PATH, body=body, headers=headers)
        response = connection.getresponse()
        raw = response.read(MAX_RESPONSE_BYTES + 1)
        if len(raw) > MAX_RESPONSE_BYTES:
            raise ValueError("market_session_response_too_large")
        response_headers = {name.lower(): value for name, value in response.getheaders()}
        return response.status, response_headers, _decode_response(
            raw, response_headers.get("content-encoding", "")
        )
    except ValueError:
        raise
    except (OSError, http.client.HTTPException, ssl.SSLError) as error:
        raise ValueError("market_session_network_failed") from error
    finally:
        connection.close()


def _conditions(value: Any) -> list[dict[str, str]]:
    if not isinstance(value, list) or len(value) > 64:
        raise ValueError("market_session_response_invalid")
    result = []
    for row in value:
        if (not isinstance(row, dict) or not {"key", "type", "value"}.issubset(row)
                or any(not isinstance(row[name], str) or not SAFE_CONDITION.fullmatch(row[name])
                       for name in ("key", "type", "value"))):
            raise ValueError("market_session_response_invalid")
        result.append({name: row[name] for name in ("key", "type", "value")})
    return result


def _normalize_listing(value: Any) -> dict[str, Any]:
    required = {
        "sale_id", "game_item_key", "game_item_quantity", "sale_price", "unit_price",
        "effective_from", "effective_to", "category_id", "grade_id", "is_set_item",
        "purchase_currency_item_key", "purchase_currency_item_key_type",
        "game_item_conditions",
    }
    if not isinstance(value, dict) or not required.issubset(value):
        raise ValueError("market_session_response_invalid")
    sale_id = value["sale_id"]
    if not isinstance(sale_id, str) or not UUID.fullmatch(sale_id):
        raise ValueError("market_session_response_invalid")
    unit_price = value["unit_price"]
    if not isinstance(unit_price, str) or not DECIMAL_TEXT.fullmatch(unit_price):
        raise ValueError("market_session_response_invalid")
    is_set = value["is_set_item"]
    if is_set not in ("0", "1", 0, 1, False, True):
        raise ValueError("market_session_response_invalid")
    return {
        "sale_id": sale_id.upper(),
        "game_item_id": _integer(value["game_item_key"], 1, 0xFFFFFFFF),
        "quantity": _integer(value["game_item_quantity"], 1, 999_999_999),
        "sale_price": _integer(value["sale_price"], 1, 999_999_999_999),
        "unit_price": unit_price,
        "effective_from": _timestamp(value["effective_from"]),
        "effective_to": _timestamp(value["effective_to"]),
        "category_id": _integer(value["category_id"], 0, 0xFFFFFFFF),
        "grade_id": _integer(value["grade_id"], 0, 0xFFFFFFFF),
        "is_set_item": bool(int(is_set)),
        "purchase_currency_item_id": _integer(
            value["purchase_currency_item_key"], 1, 0xFFFFFFFF
        ),
        "purchase_currency_type": _integer(
            value["purchase_currency_item_key_type"], 0, 0xFFFFFFFF
        ),
        "conditions": _conditions(value["game_item_conditions"]),
    }


def _normalize_response(status: int, headers: dict[str, str], data: bytes) -> dict[str, Any]:
    if status in (401, 403):
        raise MarketSessionAuthRejected(status)
    if status != 200:
        raise ValueError("market_session_http_rejected")
    content_type = headers.get("content-type", "").split(";", 1)[0].strip().lower()
    if content_type not in ("application/json", "text/json", ""):
        raise ValueError("market_session_response_not_json")
    try:
        root = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError("market_session_response_not_json") from error
    if not isinstance(root, dict) or not isinstance(root.get("sales"), list):
        raise ValueError("market_session_response_invalid")
    sales = root["sales"]
    if len(sales) > MAX_LISTINGS:
        raise ValueError("market_session_response_invalid")
    has_next = root.get("has_next_page")
    if has_next not in ("0", "1", 0, 1, False, True):
        raise ValueError("market_session_response_invalid")
    return {
        "status_code": status,
        "has_next_page": bool(int(has_next)),
        "listing_count": len(sales),
        "listings": [_normalize_listing(row) for row in sales],
    }


def _safe_request(value: dict[str, Any]) -> dict[str, Any]:
    return {
        "game_server_id": _integer(value["game_server_id"], 1, 0xFFFFFFFF),
        "game_item_ids": [_integer(item, 1, 0xFFFFFFFF)
                          for item in value["game_item_keys"].split(",")],
        "page": value["page_no"],
        "page_size": value["page_size"],
        "sort": dict(value["sort"]),
        "search": tls_contract.safe_shape(value.get("search", []), "search"),
    }


def execute_probe(
    template: dict[str, Any],
    sender: Callable[[dict[str, str], bytes], tuple[int, dict[str, str], bytes]] = _https_post,
) -> dict[str, Any]:
    body = json.dumps(template["body"], ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    try:
        status, response_headers, response_body = sender(template["headers"], body)
        normalized = _normalize_response(status, response_headers, response_body)
        report = {
            "format": FORMAT,
            "authority": AUTHORITY,
            "method": METHOD,
            "path": PATH,
            "request": _safe_request(template["body"]),
            "response": normalized,
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


def validate(report: Any) -> dict[str, Any]:
    if not isinstance(report, dict) or set(report) != {
        "format", "authority", "method", "path", "request", "response", "auth", "privacy"
    }:
        raise ValueError("market_session_response_invalid")
    if (report["format"] != FORMAT or report["authority"] != AUTHORITY
            or report["method"] != METHOD or report["path"] != PATH):
        raise ValueError("market_session_response_invalid")
    request = report["request"]
    if (not isinstance(request, dict) or set(request) != {
        "game_server_id", "game_item_ids", "page", "page_size", "sort", "search"
    } or type(request["game_server_id"]) is not int
            or not isinstance(request["game_item_ids"], list)
            or not 1 <= len(request["game_item_ids"]) <= 100
            or request["sort"].get("key") not in tls_contract.SAFE_MARKET_SORT_KEYS
            or request["sort"].get("order_by") not in tls_contract.SAFE_MARKET_SORT_ORDERS):
        raise ValueError("market_session_response_invalid")
    response = report["response"]
    if (not isinstance(response, dict) or set(response) != {
        "status_code", "has_next_page", "listing_count", "listings"
    } or response["status_code"] != 200 or type(response["has_next_page"]) is not bool
            or type(response["listing_count"]) is not int
            or not isinstance(response["listings"], list)
            or response["listing_count"] != len(response["listings"])
            or response["listing_count"] > MAX_LISTINGS):
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
    encoded = json.dumps(report, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    if len(encoded.encode("utf-8")) > MAX_REPORT_BYTES:
        raise ValueError("market_session_report_too_large")
    lowered = encoded.lower()
    for forbidden in ("authorization", "client-context", "client-game-context", "device-tag",
                      "access_token", "cookie", "seller", "account", "player"):
        if forbidden in lowered:
            raise ValueError("market_session_response_invalid")
    return report


def analyze(bundle: Path) -> dict[str, Any]:
    template = _extract_template(bundle)
    return execute_probe(template)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    try:
        report = analyze(args.input)
        if args.out.exists() or args.out.is_symlink():
            raise ValueError("market_session_request_invalid")
        with args.out.open("x", encoding="utf-8", newline="\n") as stream:
            json.dump(report, stream, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            stream.write("\n")
        return 0
    except (OSError, ValueError, json.JSONDecodeError) as error:
        code = str(error) if str(error) in SAFE_ERRORS else "market_session_network_failed"
        print(json.dumps({
            "schema_version": "aion2.market-session-probe-error.v1",
            "error": code,
            "credentials_exported": False,
            "raw_exposed": False,
            "paths_exposed": False,
        }, sort_keys=True), file=__import__("sys").stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
