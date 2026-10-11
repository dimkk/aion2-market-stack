#!/usr/bin/env python3
"""Scan one bounded, resumable AION2 market target batch with a live session JWT."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re
import time
from typing import Any, Callable, Iterable

import market_session_probe as probe


FORMAT = "aion2_market_session_scan_v2"
MULTI_FORMAT = "aion2_market_multi_session_scan_v1"
BATCH_FORMAT = "aion2_market_target_batch_v1"
MULTI_BATCH_FORMAT = "aion2_market_multi_target_batch_v1"
DEFAULT_TARGETS = (
    {"item_id": 516150001, "start_page": 1},
    {"item_id": 516140001, "start_page": 1},
    {"item_id": 110430006, "start_page": 1},
    {"item_id": 110430011, "start_page": 1},
    {"item_id": 110430079, "start_page": 1},
)
# Compatibility alias for callers that only need the original smoke targets.
TARGET_ITEM_IDS = tuple(target["item_id"] for target in DEFAULT_TARGETS)
MAX_TARGETS = 2000
# One item remains the proven/default wire shape.  A target batch may request
# a larger bounded group so the accepted service limit can be measured without
# changing code between probes.  The service previously rejected 100 keys;
# smaller values must be proven live before production use.
DEFAULT_ITEMS_PER_REQUEST = 1
MAX_ITEMS_PER_REQUEST = 100
MAX_REQUESTS = 30
MIN_INTERVAL_SECONDS = 1.0
MAX_LISTINGS = 1500
MAX_REPORT_BYTES = 1024 * 1024
MAX_MULTI_REPORT_BYTES = 32 * 1024 * 1024
MAX_SERVER_TARGETS = 100
HEX_64 = re.compile(r"^[0-9a-f]{64}$")
SAFE_ERRORS = probe.SAFE_ERRORS | frozenset({
    "market_scan_target_batch_invalid",
    "market_scan_server_mismatch",
    "market_scan_listing_scope_invalid",
    "market_scan_report_too_large",
    "market_multi_scan_target_batch_invalid",
    "market_multi_scan_report_too_large",
})


def _target_list(targets: Iterable[dict[str, Any]]) -> list[dict[str, int]]:
    normalized: list[dict[str, int]] = []
    seen: set[int] = set()
    for target in targets:
        if (
            not isinstance(target, dict)
            or set(target) != {"item_id", "start_page"}
            or type(target.get("item_id")) is not int
            or target["item_id"] <= 0
            or type(target.get("start_page")) is not int
            or not 1 <= target["start_page"] <= 100000
            or target["item_id"] in seen
        ):
            raise ValueError("market_scan_target_batch_invalid")
        seen.add(target["item_id"])
        normalized.append({
            "item_id": target["item_id"],
            "start_page": target["start_page"],
        })
    if not 1 <= len(normalized) <= MAX_TARGETS:
        raise ValueError("market_scan_target_batch_invalid")
    return normalized


def _items_per_request(document: dict[str, Any], error: str) -> int:
    value = document.get("items_per_request", DEFAULT_ITEMS_PER_REQUEST)
    if type(value) is not int or not 1 <= value <= MAX_ITEMS_PER_REQUEST:
        raise ValueError(error)
    return value


def load_target_batch(
    path: Path,
) -> tuple[list[dict[str, int]], str, str, int | None, int]:
    raw = path.read_bytes()
    if len(raw) > 256 * 1024:
        raise ValueError("market_scan_target_batch_invalid")
    document = json.loads(raw.decode("utf-8"))
    if (
        not isinstance(document, dict)
        or set(document) not in ({
            "format", "generated_at", "source_catalog_sha256", "items"
        }, {
            "format", "generated_at", "source_catalog_sha256", "items_per_request", "items"
        }, {
            "format", "generated_at", "source_catalog_sha256", "game_server_id", "items"
        }, {
            "format", "generated_at", "source_catalog_sha256", "game_server_id",
            "items_per_request", "items"
        })
        or document.get("format") != BATCH_FORMAT
        or not isinstance(document.get("generated_at"), str)
        or not HEX_64.fullmatch(str(document.get("source_catalog_sha256", "")))
        or "game_server_id" in document and (
            type(document["game_server_id"]) is not int or document["game_server_id"] <= 0
        )
    ):
        raise ValueError("market_scan_target_batch_invalid")
    targets = _target_list(document.get("items", []))
    return (
        targets, hashlib.sha256(raw).hexdigest(), document["source_catalog_sha256"],
        document.get("game_server_id"), _items_per_request(
            document, "market_scan_target_batch_invalid"
        ),
    )


def load_multi_target_batch(
    path: Path,
) -> tuple[list[dict[str, Any]], str, str, str, int]:
    raw = path.read_bytes()
    if len(raw) > 1024 * 1024:
        raise ValueError("market_multi_scan_target_batch_invalid")
    document = json.loads(raw.decode("utf-8"))
    if (
        not isinstance(document, dict)
        or set(document) not in ({
            "format", "generated_at", "source_catalog_sha256",
            "registry_sha256", "summary_report_sha256", "scopes",
        }, {
            "format", "generated_at", "source_catalog_sha256", "registry_sha256",
            "summary_report_sha256", "items_per_request", "scopes",
        })
        or document.get("format") != MULTI_BATCH_FORMAT
        or not isinstance(document.get("generated_at"), str)
        or not HEX_64.fullmatch(str(document.get("source_catalog_sha256", "")))
        or not HEX_64.fullmatch(str(document.get("registry_sha256", "")))
        or not HEX_64.fullmatch(str(document.get("summary_report_sha256", "")))
        or not isinstance(document.get("scopes"), list)
        or not 1 <= len(document["scopes"]) <= MAX_SERVER_TARGETS
    ):
        raise ValueError("market_multi_scan_target_batch_invalid")
    scopes: list[dict[str, Any]] = []
    seen: set[int] = set()
    total = 0
    for scope in document["scopes"]:
        if (
            not isinstance(scope, dict)
            or set(scope) != {"game_server_id", "items"}
            or type(scope.get("game_server_id")) is not int
            or scope["game_server_id"] <= 0
            or scope["game_server_id"] in seen
        ):
            raise ValueError("market_multi_scan_target_batch_invalid")
        targets = _target_list(scope.get("items", []))
        seen.add(scope["game_server_id"])
        total += len(targets)
        scopes.append({"game_server_id": scope["game_server_id"], "items": targets})
    if total > MAX_TARGETS:
        raise ValueError("market_multi_scan_target_batch_invalid")
    return (
        scopes, hashlib.sha256(raw).hexdigest(),
        document["source_catalog_sha256"], document["registry_sha256"],
        _items_per_request(document, "market_multi_scan_target_batch_invalid"),
    )


def execute_scan(
    template: dict[str, Any],
    sender: Callable[[dict[str, str], bytes], tuple[int, dict[str, str], bytes]] = probe._https_post,
    sleeper: Callable[[float], None] = time.sleep,
    targets: Iterable[dict[str, Any]] = DEFAULT_TARGETS,
    target_batch_sha256: str | None = None,
    source_catalog_sha256: str | None = None,
    expected_game_server_id: int | None = None,
    target_game_server_id: int | None = None,
    items_per_request: int = DEFAULT_ITEMS_PER_REQUEST,
) -> dict[str, Any]:
    target_list = _target_list(targets)
    if target_batch_sha256 is not None and not HEX_64.fullmatch(target_batch_sha256):
        raise ValueError("market_scan_target_batch_invalid")
    if source_catalog_sha256 is not None and not HEX_64.fullmatch(source_catalog_sha256):
        raise ValueError("market_scan_target_batch_invalid")
    if type(items_per_request) is not int or not 1 <= items_per_request <= MAX_ITEMS_PER_REQUEST:
        raise ValueError("market_scan_target_batch_invalid")
    headers = template["headers"]
    source = probe._request_body(json.dumps(template["body"]).encode("utf-8"))
    if expected_game_server_id is not None and target_game_server_id is not None:
        raise ValueError("market_scan_server_mismatch")
    if (
        expected_game_server_id is not None
        and int(source["game_server_id"]) != expected_game_server_id
    ):
        raise ValueError("market_scan_server_mismatch")
    if (
        target_game_server_id is not None
        and (type(target_game_server_id) is not int or target_game_server_id <= 0)
    ):
        raise ValueError("market_scan_server_mismatch")
    request_game_server_id = (
        target_game_server_id
        if target_game_server_id is not None
        else int(source["game_server_id"])
    )
    page_size = min(100, source["page_size"])
    sort = dict(source["sort"])
    listings: list[dict[str, Any]] = []
    pages: list[dict[str, Any]] = []
    request_count = 0
    complete = True
    try:
        groups: list[tuple[list[int], int]] = []
        for target in target_list:
            if (
                not groups
                or groups[-1][1] != target["start_page"]
                or len(groups[-1][0]) >= items_per_request
            ):
                groups.append(([], target["start_page"]))
            groups[-1][0].append(target["item_id"])
        for item_ids, start_page in groups:
            page_no = start_page
            while True:
                # Stop at a durable page boundary. The database resumes this
                # exact item/page during the next owner-triggered bounded run.
                if (
                    request_count >= MAX_REQUESTS
                    or len(listings) > MAX_LISTINGS - page_size
                ):
                    complete = False
                    break
                if request_count:
                    sleeper(MIN_INTERVAL_SECONDS)
                body = {
                    "game_server_id": str(request_game_server_id),
                    "game_item_keys": ",".join(str(item_id) for item_id in item_ids),
                    "page_no": page_no,
                    "page_size": page_size,
                    "sort": sort,
                }
                encoded = json.dumps(
                    body, ensure_ascii=False, separators=(",", ":")
                ).encode("utf-8")
                status, response_headers, response_body = sender(headers, encoded)
                normalized = probe._normalize_response(
                    status, response_headers, response_body
                )
                request_count += 1
                if any(row["game_item_id"] not in item_ids for row in normalized["listings"]):
                    raise ValueError("market_scan_listing_scope_invalid")
                listings.extend(normalized["listings"])
                if len(listings) > MAX_LISTINGS:
                    raise ValueError("market_scan_report_too_large")
                pages.append({
                    "item_ids": item_ids,
                    "page": page_no,
                    "listing_count": normalized["listing_count"],
                    "has_next_page": normalized["has_next_page"],
                })
                if not normalized["has_next_page"]:
                    break
                page_no += 1
            if not complete:
                break
        report = {
            "format": FORMAT,
            "authority": probe.AUTHORITY,
            "method": probe.METHOD,
            "path": probe.PATH,
            "request": {
                "game_server_id": request_game_server_id,
                "targets": target_list,
                "game_item_ids": [target["item_id"] for target in target_list],
                "page": 1,
                "page_size": page_size,
                "sort": sort,
                "minimum_interval_seconds": MIN_INTERVAL_SECONDS,
                "items_per_request": items_per_request,
            },
            "response": {
                "status_code": 200,
                "complete": complete,
                "request_count": request_count,
                "page_count": len(pages),
                "listing_count": len(listings),
                "has_next_page": not complete,
                "pages": pages,
                "listings": listings,
            },
            "auth": {
                "kind": "client_session_jwt_memory_only",
                "client_must_remain_open": True,
                "persisted": False,
                "exported": False,
            },
            "privacy": {
                "fresh_https_request": True,
                "max_requests": MAX_REQUESTS,
                "credentials_exported": False,
                "raw_headers_exported": False,
                "raw_bodies_exported": False,
                "paths_exposed": False,
            },
        }
        if target_batch_sha256 is not None:
            report["target_batch_sha256"] = target_batch_sha256
            report["source_catalog_sha256"] = source_catalog_sha256
        return validate(report)
    finally:
        headers.clear()


def validate(report: Any) -> dict[str, Any]:
    if isinstance(report, dict) and report.get("format") == MULTI_FORMAT:
        return validate_multi(report)
    base_fields = {
        "format", "authority", "method", "path", "request", "response", "auth", "privacy"
    }
    if not isinstance(report, dict) or set(report) not in (
        base_fields,
        base_fields | {"target_batch_sha256", "source_catalog_sha256"},
    ):
        raise ValueError("market_session_response_invalid")
    if (
        report["format"] != FORMAT
        or report["authority"] != probe.AUTHORITY
        or report["method"] != probe.METHOD
        or report["path"] != probe.PATH
    ):
        raise ValueError("market_session_response_invalid")
    if "target_batch_sha256" in report and (
        not HEX_64.fullmatch(str(report["target_batch_sha256"]))
        or not HEX_64.fullmatch(str(report["source_catalog_sha256"]))
    ):
        raise ValueError("market_session_response_invalid")
    request, response = report["request"], report["response"]
    try:
        targets = _target_list(request.get("targets", []))
    except ValueError as error:
        raise ValueError("market_session_response_invalid") from error
    item_ids = [target["item_id"] for target in targets]
    if (
        request.get("game_item_ids") != item_ids
        or type(request.get("game_server_id")) is not int
        or request.get("page") != 1
        or request.get("page_size") not in range(1, 101)
        or request.get("minimum_interval_seconds") != MIN_INTERVAL_SECONDS
        or type(request.get("items_per_request")) is not int
        or not 1 <= request["items_per_request"] <= MAX_ITEMS_PER_REQUEST
        or request.get("sort", {}).get("key") not in probe.tls_contract.SAFE_MARKET_SORT_KEYS
        or request.get("sort", {}).get("order_by") not in probe.tls_contract.SAFE_MARKET_SORT_ORDERS
        or not isinstance(response, dict)
        or response.get("status_code") != 200
        or type(response.get("complete")) is not bool
        or type(response.get("request_count")) is not int
        or not 1 <= response["request_count"] <= MAX_REQUESTS
        or response.get("page_count") != response["request_count"]
        or not isinstance(response.get("pages"), list)
        or len(response["pages"]) != response["page_count"]
        or not isinstance(response.get("listings"), list)
        or response.get("listing_count") != len(response["listings"])
        or response["listing_count"] > MAX_LISTINGS
        or response.get("has_next_page") is response.get("complete")
    ):
        raise ValueError("market_session_response_invalid")
    target_ids = set(item_ids)
    starts = {target["item_id"]: target["start_page"] for target in targets}
    previous: dict[tuple[int, ...], int] = {}
    for page in response["pages"]:
        page_items = page.get("item_ids") if isinstance(page, dict) else None
        group_key = tuple(page_items) if isinstance(page_items, list) else ()
        if (
            not isinstance(page, dict)
            or set(page) != {"item_ids", "page", "listing_count", "has_next_page"}
            or not 1 <= len(group_key) <= request["items_per_request"]
            or any(type(item_id) is not int or item_id not in target_ids for item_id in group_key)
            or len(set(group_key)) != len(group_key)
            or len({starts[item_id] for item_id in group_key}) != 1
            or type(page.get("page")) is not int
            or page["page"] < starts[group_key[0]]
            or page["page"] != previous.get(group_key, page["page"] - 1) + 1
            or type(page.get("listing_count")) is not int
            or page["listing_count"] < 0
            or type(page.get("has_next_page")) is not bool
        ):
            raise ValueError("market_session_response_invalid")
        previous[group_key] = page["page"]
    if any(row.get("game_item_id") not in target_ids for row in response["listings"]):
        raise ValueError("market_scan_listing_scope_invalid")
    if report["auth"] != {
        "kind": "client_session_jwt_memory_only",
        "client_must_remain_open": True,
        "persisted": False,
        "exported": False,
    } or report["privacy"] != {
        "fresh_https_request": True,
        "max_requests": MAX_REQUESTS,
        "credentials_exported": False,
        "raw_headers_exported": False,
        "raw_bodies_exported": False,
        "paths_exposed": False,
    }:
        raise ValueError("market_session_response_invalid")
    encoded = json.dumps(report, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    if len(encoded.encode("utf-8")) > MAX_REPORT_BYTES:
        raise ValueError("market_scan_report_too_large")
    lowered = encoded.lower()
    for forbidden in (
        "authorization", "client-context", "client-game-context", "device-tag",
        "access_token", "cookie", "seller", "account", "player",
    ):
        if forbidden in lowered:
            raise ValueError("market_session_response_invalid")
    return report


def execute_multi_scan(
    template: dict[str, Any],
    scopes: list[dict[str, Any]],
    target_batch_sha256: str,
    source_catalog_sha256: str,
    registry_sha256: str,
    sender: Callable[[dict[str, str], bytes], tuple[int, dict[str, str], bytes]] = probe._https_post,
    sleeper: Callable[[float], None] = time.sleep,
    items_per_request: int = DEFAULT_ITEMS_PER_REQUEST,
) -> dict[str, Any]:
    if (
        not 1 <= len(scopes) <= MAX_SERVER_TARGETS
        or not HEX_64.fullmatch(target_batch_sha256)
        or not HEX_64.fullmatch(source_catalog_sha256)
        or not HEX_64.fullmatch(registry_sha256)
    ):
        raise ValueError("market_multi_scan_target_batch_invalid")
    reports: list[dict[str, Any]] = []
    total_requests = 0
    total_listings = 0
    try:
        for index, scope in enumerate(scopes):
            if index:
                sleeper(MIN_INTERVAL_SECONDS)
            child = execute_scan(
                {"headers": dict(template["headers"]), "body": dict(template["body"])},
                sender=sender,
                sleeper=sleeper,
                targets=scope["items"],
                expected_game_server_id=None,
                target_game_server_id=scope["game_server_id"],
                items_per_request=items_per_request,
            )
            reports.append(child)
            total_requests += child["response"]["request_count"]
            total_listings += child["response"]["listing_count"]
        report = {
            "format": MULTI_FORMAT,
            "authority": probe.AUTHORITY,
            "method": probe.METHOD,
            "path": probe.PATH,
            "request": {
                "game_server_ids": [scope["game_server_id"] for scope in scopes],
                "target_count": sum(len(scope["items"]) for scope in scopes),
                "minimum_interval_seconds": MIN_INTERVAL_SECONDS,
                "items_per_request": items_per_request,
            },
            "responses": reports,
            "request_count": total_requests,
            "listing_count": total_listings,
            "target_batch_sha256": target_batch_sha256,
            "source_catalog_sha256": source_catalog_sha256,
            "registry_sha256": registry_sha256,
            "auth": {
                "kind": "client_session_jwt_memory_only",
                "client_must_remain_open": True,
                "persisted": False,
                "exported": False,
            },
            "privacy": {
                "fresh_https_request": True,
                "max_requests": MAX_SERVER_TARGETS * MAX_REQUESTS,
                "credentials_exported": False,
                "raw_headers_exported": False,
                "raw_bodies_exported": False,
                "paths_exposed": False,
            },
        }
        return validate_multi(report)
    finally:
        template.get("headers", {}).clear()


def validate_multi(report: Any) -> dict[str, Any]:
    expected = {
        "format", "authority", "method", "path", "request", "responses",
        "request_count", "listing_count", "target_batch_sha256",
        "source_catalog_sha256", "registry_sha256", "auth", "privacy",
    }
    if not isinstance(report, dict) or set(report) != expected:
        raise ValueError("market_session_response_invalid")
    request = report["request"]
    responses = report["responses"]
    server_ids = request.get("game_server_ids") if isinstance(request, dict) else None
    if (
        report["format"] != MULTI_FORMAT
        or report["authority"] != probe.AUTHORITY
        or report["method"] != probe.METHOD
        or report["path"] != probe.PATH
        or set(request) != {
            "game_server_ids", "target_count", "minimum_interval_seconds",
            "items_per_request"
        }
        or not isinstance(server_ids, list)
        or not 1 <= len(server_ids) <= MAX_SERVER_TARGETS
        or len(set(server_ids)) != len(server_ids)
        or any(type(value) is not int or value <= 0 for value in server_ids)
        or request["minimum_interval_seconds"] != MIN_INTERVAL_SECONDS
        or type(request.get("items_per_request")) is not int
        or not 1 <= request["items_per_request"] <= MAX_ITEMS_PER_REQUEST
        or type(request["target_count"]) is not int
        or request["target_count"] < len(server_ids)
        or not isinstance(responses, list)
        or len(responses) != len(server_ids)
        or any(not HEX_64.fullmatch(str(report[field])) for field in (
            "target_batch_sha256", "source_catalog_sha256", "registry_sha256"
        ))
    ):
        raise ValueError("market_session_response_invalid")
    requests = listings = targets = 0
    for expected_server_id, child in zip(server_ids, responses):
        validate(child)
        if child["request"]["game_server_id"] != expected_server_id:
            raise ValueError("market_session_response_invalid")
        requests += child["response"]["request_count"]
        listings += child["response"]["listing_count"]
        targets += len(child["request"]["targets"])
    if (
        report["request_count"] != requests
        or report["listing_count"] != listings
        or request["target_count"] != targets
        or report["auth"] != {
            "kind": "client_session_jwt_memory_only",
            "client_must_remain_open": True,
            "persisted": False,
            "exported": False,
        }
        or report["privacy"] != {
            "fresh_https_request": True,
            "max_requests": MAX_SERVER_TARGETS * MAX_REQUESTS,
            "credentials_exported": False,
            "raw_headers_exported": False,
            "raw_bodies_exported": False,
            "paths_exposed": False,
        }
    ):
        raise ValueError("market_session_response_invalid")
    encoded = json.dumps(report, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    if len(encoded.encode("utf-8")) > MAX_MULTI_REPORT_BYTES:
        raise ValueError("market_multi_scan_report_too_large")
    return report


def analyze(bundle: Path, target_batch: Path | None = None) -> dict[str, Any]:
    if target_batch is None:
        return execute_scan(probe._extract_template(bundle))
    raw = json.loads(target_batch.read_text(encoding="utf-8"))
    if isinstance(raw, dict) and raw.get("format") == MULTI_BATCH_FORMAT:
        scopes, batch_sha256, source_catalog_sha256, registry_sha256, items_per_request = (
            load_multi_target_batch(target_batch)
        )
        return execute_multi_scan(
            probe._extract_template(bundle), scopes, batch_sha256,
            source_catalog_sha256, registry_sha256,
            items_per_request=items_per_request,
        )
    (
        targets, batch_sha256, source_catalog_sha256, target_game_server_id,
        items_per_request,
    ) = load_target_batch(target_batch)
    return execute_scan(
        probe._extract_template(bundle),
        targets=targets,
        target_batch_sha256=batch_sha256,
        source_catalog_sha256=source_catalog_sha256,
        target_game_server_id=target_game_server_id,
        items_per_request=items_per_request,
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
            json.dump(report, stream, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            stream.write("\n")
        return 0
    except (OSError, ValueError, json.JSONDecodeError) as error:
        code = str(error) if str(error) in SAFE_ERRORS else "market_session_network_failed"
        print(json.dumps({
            "schema_version": "aion2.market-session-scan-error.v1",
            "error": code,
            "credentials_exported": False,
            "raw_exposed": False,
            "paths_exposed": False,
        }), file=__import__("sys").stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
