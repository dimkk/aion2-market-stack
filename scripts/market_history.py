#!/usr/bin/env python3
"""Persist AION2 market scans, plan resumable batches and render a static UI."""

from __future__ import annotations

import argparse
import datetime as dt
from decimal import Decimal, InvalidOperation
import hashlib
import html
import json
from pathlib import Path
import re
import sqlite3
import tempfile
from typing import Any


AUTHORITY = "qos-api.g.nc.com"
PATH = "/trade/v1.0/sales/valid/pages"
SUMMARY_PATH = "/trade/v1.0/sales/valid/summary/min_unit_price"
SUMMARY_FORMAT = "aion2_market_summary_probe_v1"
MULTI_SUMMARY_FORMAT = "aion2_market_multi_summary_probe_v1"
MAX_SUMMARY_TRANSPORT_ATTEMPTS_PER_SCOPE = 3
CATALOG_FORMAT = "aion2_item_catalog_prefix_v1"
BATCH_FORMAT = "aion2_market_target_batch_v1"
KNOWN_ITEMS = {
    516150001: "Kina Chest: 400,000 Kina",
    516140001: "Kina Chest: 2,000,000 Kina",
    110430006: "Fantasy Bow",
    110430011: "Courage Bow",
    110430079: "Courage Bow",
}
SCAN_STATES = frozenset({
    "pending", "partial", "scanned_empty", "scanned_listed", "error"
})


def _utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z")


def _sha256(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _report(document: Any) -> dict[str, Any]:
    if not isinstance(document, dict):
        raise ValueError("market_history_report_invalid")
    report = document.get("report", document)
    if not isinstance(report, dict):
        raise ValueError("market_history_report_invalid")
    privacy = report.get("privacy", {})
    response = report.get("response", {})
    auth = report.get("auth", {})
    if (
        report.get("authority") != AUTHORITY
        or report.get("method") != "POST"
        or report.get("path") != PATH
        or privacy.get("fresh_https_request") is not True
        or privacy.get("credentials_exported") is not False
        or privacy.get("raw_headers_exported") is not False
        or privacy.get("raw_bodies_exported") is not False
        or auth.get("persisted") is not False
        or auth.get("exported") is not False
        or response.get("status_code") != 200
        or not isinstance(response.get("listings"), list)
        or response.get("listing_count") != len(response["listings"])
    ):
        raise ValueError("market_history_report_invalid")
    request = report.get("request", {})
    item_ids = request.get("game_item_ids")
    if (
        not isinstance(request, dict)
        or type(request.get("game_server_id")) is not int
        or not isinstance(item_ids, list)
        or not item_ids
        or any(type(item_id) is not int or item_id <= 0 for item_id in item_ids)
        or len(set(item_ids)) != len(item_ids)
    ):
        raise ValueError("market_history_report_invalid")
    targets = request.get("targets")
    if targets is not None and (
        not isinstance(targets, list)
        or [target.get("item_id") for target in targets if isinstance(target, dict)] != item_ids
        or any(
            not isinstance(target, dict)
            or set(target) != {"item_id", "start_page"}
            or type(target.get("start_page")) is not int
            or target["start_page"] < 1
            for target in targets
        )
    ):
        raise ValueError("market_history_report_invalid")
    return report


def _summary_price(value: Any) -> str:
    if not isinstance(value, (str, int, float)) or isinstance(value, bool):
        raise ValueError("market_summary_report_invalid")
    try:
        number = Decimal(str(value))
    except InvalidOperation as error:
        raise ValueError("market_summary_report_invalid") from error
    if not number.is_finite() or number < 0 or number > Decimal("999999999999999"):
        raise ValueError("market_summary_report_invalid")
    return format(number, "f")


def _summary_report(document: Any) -> dict[str, Any]:
    if not isinstance(document, dict):
        raise ValueError("market_summary_report_invalid")
    report = document.get("report", document)
    if not isinstance(report, dict):
        raise ValueError("market_summary_report_invalid")
    request = report.get("request", {})
    response = report.get("response", {})
    auth = report.get("auth", {})
    privacy = report.get("privacy", {})
    rows = response.get("summaries") if isinstance(response, dict) else None
    if (
        report.get("format") != SUMMARY_FORMAT
        or report.get("authority") != AUTHORITY
        or report.get("method") != "POST"
        or report.get("path") != SUMMARY_PATH
        or not isinstance(request, dict)
        or set(request) != {"game_server_id"}
        or type(request.get("game_server_id")) is not int
        or request["game_server_id"] <= 0
        or not isinstance(response, dict)
        or response.get("status_code") != 200
        or not isinstance(rows, list)
        or response.get("summary_count") != len(rows)
        or len(rows) > 10_000
        or auth != {
            "kind": "client_session_jwt_memory_only",
            "client_must_remain_open": True,
            "persisted": False,
            "exported": False,
        }
        or privacy != {
            "fresh_https_request": True,
            "max_requests": 1,
            "credentials_exported": False,
            "raw_headers_exported": False,
            "raw_bodies_exported": False,
            "paths_exposed": False,
        }
    ):
        raise ValueError("market_summary_report_invalid")
    for row in rows:
        if (
            not isinstance(row, dict)
            or set(row) != {"game_item_id", "min_unit_price", "sale_count", "conditions"}
            or type(row.get("game_item_id")) is not int
            or row["game_item_id"] <= 0
            or type(row.get("sale_count")) is not int
            or not 0 <= row["sale_count"] <= 0xFFFFFFFF
            or not isinstance(row.get("conditions"), list)
            or len(row["conditions"]) > 64
            or any(
                not isinstance(condition, dict)
                or set(condition) != {"key", "type", "value"}
                or any(
                    not isinstance(condition[name], str)
                    or len(condition[name]) > 128
                    for name in ("key", "type", "value")
                )
                for condition in row["conditions"]
            )
        ):
            raise ValueError("market_summary_report_invalid")
        _summary_price(row["min_unit_price"])
    return report


def _targets(report: dict[str, Any]) -> list[dict[str, int]]:
    request = report["request"]
    if isinstance(request.get("targets"), list):
        return request["targets"]
    start_page = request.get("page", 1)
    return [
        {"item_id": item_id, "start_page": start_page}
        for item_id in request["game_item_ids"]
    ]


def _ensure_column(
    connection: sqlite3.Connection, table: str, column: str, declaration: str
) -> None:
    names = {row[1] for row in connection.execute(f"PRAGMA table_info({table})")}
    if column not in names:
        connection.execute(f"ALTER TABLE {table} ADD COLUMN {column} {declaration}")


def _connect(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path)
    connection.execute("PRAGMA foreign_keys=ON")
    connection.execute("PRAGMA journal_mode=WAL")
    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS scans (
          scan_id INTEGER PRIMARY KEY,
          source_sha256 TEXT NOT NULL UNIQUE,
          observed_at TEXT NOT NULL,
          authority TEXT NOT NULL,
          game_server_id INTEGER NOT NULL,
          page_no INTEGER NOT NULL,
          page_size INTEGER NOT NULL,
          listing_count INTEGER NOT NULL,
          has_next_page INTEGER NOT NULL CHECK(has_next_page IN (0,1))
        );
        CREATE TABLE IF NOT EXISTS listings (
          scan_id INTEGER NOT NULL REFERENCES scans(scan_id) ON DELETE CASCADE,
          sale_id TEXT NOT NULL,
          item_id INTEGER NOT NULL,
          item_name TEXT NOT NULL,
          quantity INTEGER NOT NULL,
          sale_price INTEGER NOT NULL,
          unit_price TEXT NOT NULL,
          effective_from TEXT NOT NULL,
          effective_to TEXT NOT NULL,
          category_id INTEGER NOT NULL,
          grade_id INTEGER NOT NULL,
          is_set_item INTEGER NOT NULL CHECK(is_set_item IN (0,1)),
          currency_item_id INTEGER NOT NULL,
          currency_type INTEGER NOT NULL,
          conditions_json TEXT NOT NULL,
          PRIMARY KEY (scan_id, sale_id)
        );
        CREATE TABLE IF NOT EXISTS catalogs (
          catalog_sha256 TEXT PRIMARY KEY,
          format TEXT NOT NULL,
          source_build INTEGER NOT NULL,
          applies_to_build INTEGER NOT NULL,
          table_version INTEGER NOT NULL,
          declared_item_count INTEGER NOT NULL,
          recovered_item_count INTEGER NOT NULL,
          imported_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS items (
          item_id INTEGER PRIMARY KEY,
          display_name TEXT NOT NULL,
          display_name_ru TEXT,
          internal_name TEXT NOT NULL,
          item_type TEXT NOT NULL,
          item_level INTEGER NOT NULL,
          icon_name TEXT,
          grade TEXT,
          tier TEXT,
          catalog_sha256 TEXT,
          scan_status TEXT NOT NULL DEFAULT 'pending',
          next_page INTEGER NOT NULL DEFAULT 1,
          current_generation INTEGER NOT NULL DEFAULT 0,
          last_scan_id INTEGER,
          last_scanned_at TEXT,
          last_listing_count INTEGER NOT NULL DEFAULT 0,
          scan_attempts INTEGER NOT NULL DEFAULT 0,
          last_error TEXT,
          priority INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS item_server_state (
          game_server_id INTEGER NOT NULL,
          item_id INTEGER NOT NULL REFERENCES items(item_id) ON DELETE CASCADE,
          scan_status TEXT NOT NULL DEFAULT 'pending',
          next_page INTEGER NOT NULL DEFAULT 1,
          current_generation INTEGER NOT NULL DEFAULT 0,
          last_scan_id INTEGER,
          last_scanned_at TEXT,
          last_listing_count INTEGER NOT NULL DEFAULT 0,
          scan_attempts INTEGER NOT NULL DEFAULT 0,
          last_error TEXT,
          PRIMARY KEY(game_server_id,item_id)
        );
        CREATE TABLE IF NOT EXISTS scan_items (
          scan_id INTEGER NOT NULL REFERENCES scans(scan_id) ON DELETE CASCADE,
          item_id INTEGER NOT NULL,
          status TEXT NOT NULL,
          start_page INTEGER NOT NULL,
          end_page INTEGER,
          page_count INTEGER NOT NULL,
          listing_count INTEGER NOT NULL,
          has_next_page INTEGER NOT NULL CHECK(has_next_page IN (0,1)),
          next_page INTEGER NOT NULL,
          generation INTEGER NOT NULL,
          error TEXT,
          PRIMARY KEY(scan_id,item_id)
        );
        CREATE TABLE IF NOT EXISTS summary_runs (
          summary_id INTEGER PRIMARY KEY,
          source_sha256 TEXT NOT NULL UNIQUE,
          observed_at TEXT NOT NULL,
          authority TEXT NOT NULL,
          game_server_id INTEGER NOT NULL,
          summary_count INTEGER NOT NULL
        );
        CREATE TABLE IF NOT EXISTS summary_prices (
          summary_id INTEGER NOT NULL REFERENCES summary_runs(summary_id) ON DELETE CASCADE,
          item_id INTEGER NOT NULL,
          min_unit_price TEXT NOT NULL,
          sale_count INTEGER NOT NULL,
          conditions_json TEXT NOT NULL,
          PRIMARY KEY(summary_id,item_id,conditions_json)
        );
        CREATE INDEX IF NOT EXISTS listings_item_scan
          ON listings(item_id, scan_id, sale_price);
        CREATE INDEX IF NOT EXISTS listings_sale_history
          ON listings(sale_id, scan_id);
        CREATE INDEX IF NOT EXISTS items_scan_queue
          ON items(scan_status,priority DESC,last_scanned_at,item_id);
        CREATE INDEX IF NOT EXISTS item_server_state_scan_queue
          ON item_server_state(game_server_id,scan_status,last_scanned_at,item_id);
        CREATE INDEX IF NOT EXISTS scan_items_item_scan
          ON scan_items(item_id,scan_id);
        CREATE INDEX IF NOT EXISTS summary_prices_item_run
          ON summary_prices(item_id,summary_id);
        """
    )
    _ensure_column(connection, "scans", "complete", "INTEGER NOT NULL DEFAULT 1")
    _ensure_column(connection, "scans", "request_count", "INTEGER NOT NULL DEFAULT 1")
    _ensure_column(connection, "scans", "target_count", "INTEGER NOT NULL DEFAULT 1")
    _ensure_column(connection, "scans", "target_batch_sha256", "TEXT")
    _ensure_column(connection, "scans", "source_catalog_sha256", "TEXT")
    _ensure_column(connection, "listings", "generation", "INTEGER NOT NULL DEFAULT 1")
    _ensure_column(connection, "items", "display_name_ru", "TEXT")
    _ensure_column(connection, "items", "icon_name", "TEXT")
    _ensure_column(connection, "items", "grade", "TEXT")
    _ensure_column(connection, "items", "tier", "TEXT")
    # Migrate legacy scan evidence into server-scoped state.  This statement is
    # deliberately idempotent and never copies one server's generation to
    # another server.
    connection.execute(
        """INSERT OR IGNORE INTO item_server_state(
             game_server_id,item_id,scan_status,next_page,current_generation,
             last_scan_id,last_scanned_at,last_listing_count,scan_attempts,last_error
           )
           WITH ranked AS (
             SELECT s.game_server_id,si.item_id,si.status,si.next_page,si.generation,
                    si.scan_id,s.observed_at,si.error,
                    row_number() OVER (
                      PARTITION BY s.game_server_id,si.item_id ORDER BY si.scan_id DESC
                    ) AS position,
                    count(*) OVER (
                      PARTITION BY s.game_server_id,si.item_id
                    ) AS attempts
             FROM scan_items si JOIN scans s USING(scan_id)
             WHERE si.status!='not_attempted'
           )
           SELECT r.game_server_id,r.item_id,r.status,r.next_page,r.generation,
                  r.scan_id,r.observed_at,
                  (SELECT count(*) FROM listings l JOIN scans ls USING(scan_id)
                   WHERE ls.game_server_id=r.game_server_id
                     AND l.item_id=r.item_id AND l.generation=r.generation),
                  r.attempts,r.error
           FROM ranked r WHERE r.position=1"""
    )
    return connection


def import_catalog(
    catalog_path: Path,
    database: Path,
    source_build: int,
    applies_to_build: int,
) -> dict[str, Any]:
    raw = catalog_path.read_bytes()
    document = json.loads(raw.decode("utf-8"))
    items = document.get("items") if isinstance(document, dict) else None
    if (
        document.get("format") != CATALOG_FORMAT
        or type(document.get("table_version")) is not int
        or type(document.get("declared_item_count")) is not int
        or type(document.get("recovered_item_count")) is not int
        or not isinstance(items, list)
        or document["recovered_item_count"] != len(items)
        or not items
    ):
        raise ValueError("market_catalog_invalid")
    normalized: list[tuple[Any, ...]] = []
    seen: set[int] = set()
    for item in items:
        item_id = item.get("id") if isinstance(item, dict) else None
        if (
            type(item_id) is not int
            or item_id <= 0
            or item_id in seen
            or not isinstance(item.get("display_name"), str)
            or not isinstance(item.get("internal_name"), str)
            or item.get("item_type") not in {"Equip", "Usable", "Misc", "Currency", "Quest"}
            or type(item.get("global_item_level")) is not int
        ):
            raise ValueError("market_catalog_invalid")
        seen.add(item_id)
        display_name = item["display_name"].strip() or item["internal_name"] or f"Item {item_id}"
        display_names = item.get("display_names")
        display_name_ru = (
            display_names.get("ru").strip()
            if isinstance(display_names, dict) and isinstance(display_names.get("ru"), str)
            else None
        )
        icon_name = item.get("icon_name") if isinstance(item.get("icon_name"), str) else None
        grade = item.get("grade") if isinstance(item.get("grade"), str) else None
        tier = item.get("tier") if isinstance(item.get("tier"), str) else None
        priority = 1000 if item_id in KNOWN_ITEMS else (
            20 if item["item_type"] == "Equip" else 10 if item["item_type"] == "Usable" else 0
        )
        normalized.append((
            item_id, KNOWN_ITEMS.get(item_id, display_name), item["internal_name"],
            display_name_ru, item["item_type"], item["global_item_level"],
            icon_name, grade, tier, priority,
        ))
    digest = _sha256(raw)
    now = _utc_now()
    connection = _connect(database)
    try:
        with connection:
            connection.execute(
                """INSERT OR IGNORE INTO catalogs VALUES(?,?,?,?,?,?,?,?)""",
                (
                    digest, CATALOG_FORMAT, source_build, applies_to_build,
                    document["table_version"], document["declared_item_count"],
                    document["recovered_item_count"], now,
                ),
            )
            connection.executemany(
                """INSERT INTO items(
                     item_id,display_name,internal_name,display_name_ru,item_type,item_level,
                     icon_name,grade,tier,catalog_sha256,priority
                   ) VALUES(?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(item_id) DO UPDATE SET
                     display_name=excluded.display_name,
                     display_name_ru=excluded.display_name_ru,
                     internal_name=excluded.internal_name,
                     item_type=excluded.item_type,
                     item_level=excluded.item_level,
                     icon_name=excluded.icon_name,
                     grade=excluded.grade,
                     tier=excluded.tier,
                     catalog_sha256=excluded.catalog_sha256,
                     priority=excluded.priority""",
                [row[:9] + (digest, row[9]) for row in normalized],
            )
            # Existing pre-catalog receipts remain authoritative scan evidence.
            connection.execute(
                """INSERT OR IGNORE INTO scan_items(
                     scan_id,item_id,status,start_page,end_page,page_count,listing_count,
                     has_next_page,next_page,generation,error
                   )
                   SELECT l.scan_id,l.item_id,
                          CASE WHEN s.has_next_page=1 THEN 'partial' ELSE 'scanned_listed' END,
                          s.page_no,s.page_no,1,count(*),s.has_next_page,
                          CASE WHEN s.has_next_page=1 THEN s.page_no+1 ELSE 1 END,1,NULL
                   FROM listings l JOIN scans s USING(scan_id)
                   GROUP BY l.scan_id,l.item_id"""
            )
            # Legacy receipts predate explicit generation/checkpoint fields.
            # Treat each completed legacy receipt as a new item snapshot so the
            # UI never merges stale rows into the current market view.
            connection.execute(
                """UPDATE listings AS current SET generation=(
                     SELECT count(DISTINCT prior.scan_id) FROM listings prior
                     JOIN scans prior_scan ON prior_scan.scan_id=prior.scan_id
                     WHERE prior.item_id=current.item_id
                       AND prior_scan.target_batch_sha256 IS NULL
                       AND prior.scan_id<=current.scan_id
                   ) WHERE current.scan_id IN(
                     SELECT scan_id FROM scans WHERE target_batch_sha256 IS NULL
                   )"""
            )
            connection.execute(
                """UPDATE scan_items AS current SET generation=COALESCE((
                     SELECT max(l.generation) FROM listings l
                     WHERE l.scan_id=current.scan_id AND l.item_id=current.item_id
                   ),generation) WHERE current.scan_id IN(
                     SELECT scan_id FROM scans WHERE target_batch_sha256 IS NULL
                   )"""
            )
            connection.execute(
                """UPDATE items SET
                     scan_status='scanned_listed',
                     current_generation=(SELECT max(l.generation) FROM listings l
                                         WHERE l.item_id=items.item_id),
                     last_listing_count=(SELECT count(*) FROM listings l
                       WHERE l.item_id=items.item_id AND l.generation=(
                         SELECT max(latest.generation) FROM listings latest
                         WHERE latest.item_id=items.item_id)),
                     last_scan_id=(SELECT max(l.scan_id) FROM listings l WHERE l.item_id=items.item_id),
                     last_scanned_at=(SELECT s.observed_at FROM listings l JOIN scans s USING(scan_id)
                                      WHERE l.item_id=items.item_id ORDER BY s.observed_at DESC LIMIT 1),
                     scan_attempts=MAX(scan_attempts,(SELECT count(DISTINCT l.scan_id) FROM listings l
                                                     WHERE l.item_id=items.item_id))
                   WHERE EXISTS(SELECT 1 FROM listings l WHERE l.item_id=items.item_id)
                     AND NOT EXISTS(
                       SELECT 1 FROM scan_items si JOIN scans s USING(scan_id)
                       WHERE si.item_id=items.item_id AND s.target_batch_sha256 IS NOT NULL
                     )"""
            )
        return {
            "status": "catalog_imported",
            "catalog_sha256": digest,
            "items": len(normalized),
            "source_build": source_build,
            "applies_to_build": applies_to_build,
        }
    finally:
        connection.close()


def plan_batch(
    database: Path,
    output: Path,
    limit: int = 2000,
    mode: str = "pending",
    game_server_id: int | None = None,
    refresh_before: str | None = None,
) -> dict[str, Any]:
    if (
        not 1 <= limit <= 2000
        or mode not in {"pending", "refresh", "summary-active"}
        or game_server_id is not None and game_server_id <= 0
        or refresh_before is not None and mode != "refresh"
    ):
        raise ValueError("market_batch_plan_invalid")
    if refresh_before is not None:
        try:
            parsed_cutoff = dt.datetime.fromisoformat(
                refresh_before.replace("Z", "+00:00")
            )
            if parsed_cutoff.tzinfo is None:
                raise ValueError("timezone_required")
            refresh_before = parsed_cutoff.astimezone(dt.timezone.utc).isoformat().replace(
                "+00:00", "Z"
            )
        except (AttributeError, TypeError, ValueError) as error:
            raise ValueError("market_batch_plan_invalid") from error
    connection = _connect(database)
    connection.row_factory = sqlite3.Row
    try:
        catalog = connection.execute(
            "SELECT catalog_sha256 FROM catalogs ORDER BY imported_at DESC LIMIT 1"
        ).fetchone()
        if catalog is None:
            raise ValueError("market_catalog_missing")
        if game_server_id is None:
            source = connection.execute(
                """SELECT game_server_id FROM (
                     SELECT game_server_id,observed_at FROM scans
                     UNION ALL SELECT game_server_id,observed_at FROM summary_runs
                   ) ORDER BY observed_at DESC LIMIT 1"""
            ).fetchone()
            game_server_id = int(source[0]) if source is not None else None
        if mode == "pending":
            if game_server_id is None:
                rows = connection.execute(
                    """SELECT item_id,1 AS next_page FROM items
                       ORDER BY priority DESC,item_id LIMIT ?""", (limit,)
                ).fetchall()
                remaining = connection.execute("SELECT count(*) FROM items").fetchone()[0]
            else:
                rows = connection.execute(
                    """SELECT i.item_id,COALESCE(st.next_page,1) AS next_page
                       FROM items i LEFT JOIN item_server_state st
                         ON st.game_server_id=? AND st.item_id=i.item_id
                       WHERE st.item_id IS NULL OR st.scan_status IN ('partial','pending','error')
                       ORDER BY CASE st.scan_status WHEN 'partial' THEN 0 WHEN 'error' THEN 1 ELSE 2 END,
                                i.priority DESC,i.item_id LIMIT ?""",
                    (game_server_id, limit),
                ).fetchall()
                remaining = connection.execute(
                    """SELECT count(*) FROM items i LEFT JOIN item_server_state st
                         ON st.game_server_id=? AND st.item_id=i.item_id
                       WHERE st.item_id IS NULL OR st.scan_status IN ('partial','pending','error')""",
                    (game_server_id,),
                ).fetchone()[0]
        elif mode == "refresh":
            if game_server_id is None:
                raise ValueError("market_plan_server_required")
            rows = connection.execute(
                """SELECT i.item_id,
                          CASE WHEN st.scan_status='partial' THEN st.next_page ELSE 1 END AS next_page
                   FROM item_server_state st
                   JOIN items i USING(item_id)
                   WHERE st.game_server_id=?
                     AND (st.scan_status IN ('partial','error') OR (
                       st.scan_status IN ('scanned_empty','scanned_listed')
                       AND (? IS NULL OR st.last_scanned_at < ?)))
                   ORDER BY CASE st.scan_status WHEN 'partial' THEN 0 WHEN 'error' THEN 1 ELSE 2 END,
                            COALESCE(st.last_scanned_at,''),i.priority DESC,i.item_id LIMIT ?""",
                (game_server_id, refresh_before, refresh_before, limit),
            ).fetchall()
            remaining = connection.execute(
                """SELECT count(*) FROM item_server_state
                   WHERE game_server_id=?
                     AND (scan_status IN ('partial','error') OR (
                       scan_status IN ('scanned_empty','scanned_listed')
                       AND (? IS NULL OR last_scanned_at < ?)))""",
                (game_server_id, refresh_before, refresh_before),
            ).fetchone()[0]
        else:
            latest = connection.execute(
                """SELECT summary_id,game_server_id FROM summary_runs
                   WHERE (? IS NULL OR game_server_id=?)
                   ORDER BY observed_at DESC,summary_id DESC LIMIT 1""",
                (game_server_id, game_server_id),
            ).fetchone()
            if latest is None:
                raise ValueError("market_summary_missing")
            latest_summary_id = int(latest["summary_id"])
            game_server_id = int(latest["game_server_id"])
            rows = connection.execute(
                """WITH latest AS (
                     SELECT summary_id,observed_at,game_server_id
                     FROM summary_runs WHERE summary_id=?
                   ), active AS (
                     SELECT sp.item_id,sum(sp.sale_count) AS summary_sale_count
                     FROM summary_prices sp,latest l
                     WHERE sp.summary_id=l.summary_id
                     GROUP BY sp.item_id HAVING sum(sp.sale_count)>0
                   ), current_state AS (
                     SELECT si.item_id,si.status,si.next_page,
                            row_number() OVER (
                              PARTITION BY si.item_id ORDER BY si.scan_id DESC
                            ) AS position
                     FROM scan_items si JOIN scans s USING(scan_id),latest l
                     WHERE s.game_server_id=l.game_server_id
                       AND s.observed_at>=l.observed_at
                       AND si.status!='not_attempted'
                   )
                   SELECT i.item_id,
                          CASE WHEN cs.status='partial' THEN cs.next_page ELSE 1 END AS next_page,
                          a.summary_sale_count
                   FROM active a JOIN items i USING(item_id)
                   LEFT JOIN current_state cs
                     ON cs.item_id=i.item_id AND cs.position=1
                   WHERE cs.item_id IS NULL OR cs.status IN ('partial','error')
                   ORDER BY CASE cs.status WHEN 'partial' THEN 0 WHEN 'error' THEN 1 ELSE 2 END,
                            a.summary_sale_count DESC,i.priority DESC,i.item_id
                   LIMIT ?""",
                (latest_summary_id, limit),
            ).fetchall()
            remaining = connection.execute(
                """WITH latest AS (
                     SELECT summary_id,observed_at,game_server_id
                     FROM summary_runs WHERE summary_id=?
                   ), active AS (
                     SELECT sp.item_id
                     FROM summary_prices sp,latest l
                     WHERE sp.summary_id=l.summary_id
                     GROUP BY sp.item_id HAVING sum(sp.sale_count)>0
                   ), covered AS (
                     SELECT DISTINCT si.item_id
                     FROM scan_items si JOIN scans s USING(scan_id),latest l
                     WHERE s.game_server_id=l.game_server_id
                       AND s.observed_at>=l.observed_at
                       AND si.status IN ('scanned_empty','scanned_listed')
                   )
                   SELECT count(*) FROM active a
                   LEFT JOIN covered c USING(item_id) WHERE c.item_id IS NULL""",
                (latest_summary_id,),
            ).fetchone()[0]
    finally:
        connection.close()
    if not rows:
        return {
            "status": (
                "traversal_complete" if mode == "pending"
                else "nothing_to_refresh" if mode == "refresh"
                else "summary_has_no_active_items"
            ),
            "items": 0,
            "remaining": 0,
        }
    document = {
        "format": BATCH_FORMAT,
        "generated_at": _utc_now(),
        "source_catalog_sha256": catalog["catalog_sha256"],
        "items": [
            {"item_id": row["item_id"], "start_page": row["next_page"]}
            for row in rows
        ],
    }
    if game_server_id is not None:
        document["game_server_id"] = game_server_id
    output.parent.mkdir(parents=True, exist_ok=True)
    if output.exists() or output.is_symlink():
        raise ValueError("market_batch_output_exists")
    encoded = json.dumps(document, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
    output.write_text(encoded, encoding="utf-8", newline="\n")
    return {
        "status": "batch_planned",
        "items": len(rows),
        "remaining": remaining,
        "batch_sha256": _sha256(encoded.encode("utf-8")),
        "output": str(output),
        "mode": mode,
        "game_server_id": game_server_id,
    }


def _fallback_item(connection: sqlite3.Connection, item_id: int) -> None:
    connection.execute(
        """INSERT OR IGNORE INTO items(
             item_id,display_name,internal_name,item_type,item_level,priority
           ) VALUES(?,?,?,?,?,?)""",
        (item_id, KNOWN_ITEMS.get(item_id, f"Item {item_id}"), "", "Misc", 0,
         1000 if item_id in KNOWN_ITEMS else 0),
    )


def _page_contains(page: dict[str, Any], item_id: int) -> bool:
    if page.get("item_id") == item_id:
        return True
    item_ids = page.get("item_ids")
    return isinstance(item_ids, list) and item_id in item_ids


def ingest(
    report_path: Path,
    database: Path,
    *,
    source_sha256: str | None = None,
    observed_at: str | None = None,
) -> dict[str, Any]:
    raw = report_path.read_bytes()
    report = _report(json.loads(raw.decode("utf-8")))
    digest = source_sha256 or _sha256(raw)
    if not re.fullmatch(r"[0-9a-f]{64}", digest):
        raise ValueError("market_history_source_sha256_invalid")
    observed = observed_at or dt.datetime.fromtimestamp(
        report_path.stat().st_mtime, tz=dt.timezone.utc
    ).isoformat().replace("+00:00", "Z")
    try:
        parsed_observed = dt.datetime.fromisoformat(observed.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("market_history_observed_at_invalid") from exc
    if parsed_observed.tzinfo is None:
        raise ValueError("market_history_observed_at_invalid")
    request, response = report["request"], report["response"]
    targets = _targets(report)
    pages = response.get("pages", [])
    if not pages and len(targets) == 1:
        pages = [{
            "item_id": targets[0]["item_id"],
            "page": targets[0]["start_page"],
            "listing_count": response["listing_count"],
            "has_next_page": bool(response.get("has_next_page", False)),
        }]
    connection = _connect(database)
    connection.row_factory = sqlite3.Row
    try:
        existing = connection.execute(
            "SELECT scan_id FROM scans WHERE source_sha256=?", (digest,)
        ).fetchone()
        if existing:
            return {
                "status": "already_ingested",
                "scan_id": existing[0],
                "listings": response["listing_count"],
                "source_sha256": digest,
            }
        with connection:
            for target in targets:
                _fallback_item(connection, target["item_id"])
                connection.execute(
                    """INSERT OR IGNORE INTO item_server_state(game_server_id,item_id)
                       VALUES(?,?)""",
                    (request["game_server_id"], target["item_id"]),
                )
            cursor = connection.execute(
                """INSERT INTO scans(
                     source_sha256,observed_at,authority,game_server_id,page_no,
                     page_size,listing_count,has_next_page,complete,request_count,
                     target_count,target_batch_sha256,source_catalog_sha256
                   ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    digest, observed, report["authority"], request["game_server_id"],
                    min(target["start_page"] for target in targets), request["page_size"],
                    response["listing_count"], int(bool(response.get("has_next_page", False))),
                    int(bool(response.get("complete", True))),
                    int(response.get("request_count", max(1, len(pages)))), len(targets),
                    report.get("target_batch_sha256"), report.get("source_catalog_sha256"),
                ),
            )
            scan_id = int(cursor.lastrowid)
            target_ids = {target["item_id"] for target in targets}
            state_by_item = {
                row["item_id"]: dict(row)
                for row in connection.execute(
                    """SELECT item_id,scan_status,current_generation,last_listing_count
                       FROM item_server_state WHERE game_server_id=?""",
                    (request["game_server_id"],),
                )
                if row["item_id"] in target_ids
            }
            generation_by_item: dict[int, int] = {}
            for target in targets:
                state = state_by_item[target["item_id"]]
                generation_by_item[target["item_id"]] = (
                    state["current_generation"] + 1
                    if target["start_page"] == 1
                    else max(1, state["current_generation"])
                )
            names = {
                row["item_id"]: row["display_name"]
                for row in connection.execute(
                    "SELECT item_id,display_name FROM items"
                )
                if row["item_id"] in target_ids
            }
            listing_rows_by_sale_id: dict[str, tuple[Any, ...]] = {}
            for listing in response["listings"]:
                item_id = int(listing["game_item_id"])
                listing_row = (
                    scan_id, listing["sale_id"], item_id,
                    names.get(item_id, f"Item {item_id}"), int(listing["quantity"]),
                    int(listing["sale_price"]), listing["unit_price"],
                    listing["effective_from"], listing["effective_to"],
                    int(listing["category_id"]), int(listing["grade_id"]),
                    int(bool(listing["is_set_item"])),
                    int(listing["purchase_currency_item_id"]),
                    int(listing["purchase_currency_type"]),
                    json.dumps(listing["conditions"], ensure_ascii=False, sort_keys=True,
                               separators=(",", ":")),
                    generation_by_item[item_id],
                )
                previous = listing_rows_by_sale_id.get(listing["sale_id"])
                if previous is not None and previous != listing_row:
                    raise ValueError("market_history_listing_identity_conflict")
                listing_rows_by_sale_id[listing["sale_id"]] = listing_row
            listing_rows = list(listing_rows_by_sale_id.values())
            connection.executemany(
                "INSERT INTO listings VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                listing_rows,
            )
            for target in targets:
                item_id, start_page = target["item_id"], target["start_page"]
                item_pages = [page for page in pages if _page_contains(page, item_id)]
                item_listing_count = sum(1 for row in listing_rows if row[2] == item_id)
                generation = generation_by_item[item_id]
                if not item_pages:
                    connection.execute(
                        """INSERT INTO scan_items VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                        (scan_id, item_id, "not_attempted", start_page, None, 0, 0, 1,
                         start_page, generation, None),
                    )
                    continue
                end_page = item_pages[-1]["page"]
                has_next = bool(item_pages[-1]["has_next_page"])
                previous_count = (
                    state_by_item[item_id]["last_listing_count"] if start_page > 1 else 0
                )
                cumulative_count = previous_count + item_listing_count
                status = "partial" if has_next else (
                    "scanned_listed" if cumulative_count else "scanned_empty"
                )
                next_page = end_page + 1 if has_next else 1
                connection.execute(
                    """INSERT INTO scan_items VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                    (scan_id, item_id, status, start_page, end_page, len(item_pages),
                     item_listing_count, int(has_next), next_page, generation, None),
                )
                connection.execute(
                    """UPDATE item_server_state
                         SET scan_status=?,next_page=?,current_generation=?,
                         last_scan_id=?,last_scanned_at=?,last_listing_count=?,
                         scan_attempts=scan_attempts+1,last_error=NULL
                         WHERE game_server_id=? AND item_id=?""",
                    (status, next_page, generation, scan_id, observed, cumulative_count,
                     request["game_server_id"], item_id),
                )
                # Compatibility projection for old local reports.  All market
                # correctness paths use item_server_state, never these fields.
                connection.execute(
                    """UPDATE items SET scan_status=?,next_page=?,current_generation=?,
                         last_scan_id=?,last_scanned_at=?,last_listing_count=?,
                         scan_attempts=scan_attempts+1,last_error=NULL WHERE item_id=?""",
                    (status, next_page, generation, scan_id, observed, cumulative_count, item_id),
                )
        return {
            "status": "ingested",
            "scan_id": scan_id,
            "targets": len(targets),
            "attempted_targets": sum(1 for target in targets if any(
                _page_contains(page, target["item_id"]) for page in pages
            )),
            "complete": bool(response.get("complete", True)),
            "listings": len(listing_rows),
            "source_sha256": digest,
        }
    finally:
        connection.close()


def ingest_summary(report_path: Path, database: Path) -> dict[str, Any]:
    raw = report_path.read_bytes()
    report = _summary_report(json.loads(raw.decode("utf-8")))
    digest = _sha256(raw)
    observed = dt.datetime.fromtimestamp(
        report_path.stat().st_mtime, tz=dt.timezone.utc
    ).isoformat().replace("+00:00", "Z")
    rows = report["response"]["summaries"]
    connection = _connect(database)
    try:
        existing = connection.execute(
            "SELECT summary_id FROM summary_runs WHERE source_sha256=?", (digest,)
        ).fetchone()
        if existing:
            return {
                "status": "already_ingested",
                "summary_id": existing[0],
                "summaries": len(rows),
                "source_sha256": digest,
            }
        normalized: list[tuple[int, str, int, str]] = []
        seen: set[tuple[int, str]] = set()
        for row in rows:
            item_id = row["game_item_id"]
            conditions = json.dumps(
                row["conditions"], ensure_ascii=False, sort_keys=True,
                separators=(",", ":"),
            )
            key = (item_id, conditions)
            if key in seen:
                raise ValueError("market_summary_report_invalid")
            seen.add(key)
            normalized.append((
                item_id, _summary_price(row["min_unit_price"]),
                row["sale_count"], conditions,
            ))
        with connection:
            for item_id, _price, _count, _conditions in normalized:
                _fallback_item(connection, item_id)
            cursor = connection.execute(
                """INSERT INTO summary_runs(
                     source_sha256,observed_at,authority,game_server_id,summary_count
                   ) VALUES(?,?,?,?,?)""",
                (
                    digest, observed, report["authority"],
                    report["request"]["game_server_id"], len(normalized),
                ),
            )
            summary_id = int(cursor.lastrowid)
            connection.executemany(
                """INSERT INTO summary_prices(
                     summary_id,item_id,min_unit_price,sale_count,conditions_json
                   ) VALUES(?,?,?,?,?)""",
                [
                    (summary_id, item_id, price, count, conditions)
                    for item_id, price, count, conditions in normalized
                ],
            )
        return {
            "status": "summary_ingested",
            "summary_id": summary_id,
            "summaries": len(normalized),
            "active_items": len({
                item_id for item_id, _price, count, _conditions in normalized
                if count > 0
            }),
            "source_sha256": digest,
        }
    finally:
        connection.close()


def ingest_multi_summary(
    report_path: Path,
    database: Path,
    *,
    observed_at: str,
    expected_sha256: str | None = None,
) -> dict[str, Any]:
    raw = report_path.read_bytes()
    source_sha256 = _sha256(raw)
    if expected_sha256 is not None and (
        re.fullmatch(r"[0-9a-f]{64}", expected_sha256) is None
        or source_sha256 != expected_sha256
    ):
        raise ValueError("market_summary_source_sha256_mismatch")
    document = json.loads(raw.decode("utf-8"))
    report = document.get("report", document) if isinstance(document, dict) else None
    if not isinstance(report, dict) or report.get("format") != MULTI_SUMMARY_FORMAT:
        raise ValueError("market_summary_report_invalid")
    request = report.get("request", {})
    responses = report.get("responses")
    server_ids = request.get("game_server_ids") if isinstance(request, dict) else None
    if (
        report.get("authority") != AUTHORITY
        or report.get("method") != "POST"
        or report.get("path") != SUMMARY_PATH
        or set(request) != {"game_server_ids", "minimum_interval_seconds"}
        or not isinstance(server_ids, list)
        or not 1 <= len(server_ids) <= 100
        or len(set(server_ids)) != len(server_ids)
        or any(type(value) is not int or value <= 0 for value in server_ids)
        or request["minimum_interval_seconds"] != 1.0
        or not isinstance(responses, list)
        or len(responses) != len(server_ids)
        or type(report.get("transport_attempts")) is not int
        or not len(server_ids) <= report["transport_attempts"]
            <= len(server_ids) * MAX_SUMMARY_TRANSPORT_ATTEMPTS_PER_SCOPE
        or report.get("auth") != {
            "kind": "client_session_jwt_memory_only",
            "client_must_remain_open": True,
            "persisted": False,
            "exported": False,
        }
        or report.get("privacy") != {
            "fresh_https_request": True,
            "max_requests": len(server_ids) * MAX_SUMMARY_TRANSPORT_ATTEMPTS_PER_SCOPE,
            "credentials_exported": False,
            "raw_headers_exported": False,
            "raw_bodies_exported": False,
            "paths_exposed": False,
        }
    ):
        raise ValueError("market_summary_report_invalid")
    try:
        parsed_observed = dt.datetime.fromisoformat(
            observed_at.replace("Z", "+00:00")
        )
    except (AttributeError, ValueError) as exc:
        raise ValueError("market_history_observed_at_invalid") from exc
    if parsed_observed.tzinfo is None or parsed_observed.utcoffset() is None:
        raise ValueError("market_history_observed_at_invalid")
    observed = parsed_observed.astimezone(dt.timezone.utc).isoformat().replace(
        "+00:00", "Z"
    )
    connection = _connect(database)
    inserted = skipped = summaries = active_items = 0
    try:
        with connection:
            for expected_server_id, response in zip(server_ids, responses):
                single = {
                    "format": SUMMARY_FORMAT,
                    "authority": AUTHORITY,
                    "method": "POST",
                    "path": SUMMARY_PATH,
                    "request": {"game_server_id": expected_server_id},
                    "response": {
                        "status_code": response.get("status_code"),
                        "summary_count": response.get("summary_count"),
                        "summaries": response.get("summaries"),
                    } if isinstance(response, dict) else {},
                    "auth": report["auth"],
                    "privacy": {**report["privacy"], "max_requests": 1},
                }
                _summary_report(single)
                if response.get("game_server_id") != expected_server_id:
                    raise ValueError("market_summary_report_invalid")
                digest = hashlib.sha256(
                    raw + b":" + str(expected_server_id).encode("ascii")
                ).hexdigest()
                existing = connection.execute(
                    "SELECT summary_id FROM summary_runs WHERE source_sha256=?", (digest,)
                ).fetchone()
                if existing:
                    skipped += 1
                    continue
                normalized: list[tuple[int, str, int, str]] = []
                seen: set[tuple[int, str]] = set()
                for row in response["summaries"]:
                    conditions = json.dumps(
                        row["conditions"], ensure_ascii=False, sort_keys=True,
                        separators=(",", ":"),
                    )
                    key = (row["game_item_id"], conditions)
                    if key in seen:
                        raise ValueError("market_summary_report_invalid")
                    seen.add(key)
                    normalized.append((
                        row["game_item_id"], _summary_price(row["min_unit_price"]),
                        row["sale_count"], conditions,
                    ))
                for item_id, _price, _count, _conditions in normalized:
                    _fallback_item(connection, item_id)
                cursor = connection.execute(
                    """INSERT INTO summary_runs(
                         source_sha256,observed_at,authority,game_server_id,summary_count
                       ) VALUES(?,?,?,?,?)""",
                    (digest, observed, AUTHORITY, expected_server_id, len(normalized)),
                )
                summary_id = int(cursor.lastrowid)
                connection.executemany(
                    """INSERT INTO summary_prices(
                         summary_id,item_id,min_unit_price,sale_count,conditions_json
                       ) VALUES(?,?,?,?,?)""",
                    [
                        (summary_id, item_id, price, count, conditions)
                        for item_id, price, count, conditions in normalized
                    ],
                )
                inserted += 1
                summaries += len(normalized)
                active_items += sum(count > 0 for _, _, count, _ in normalized)
        return {
            "status": "multi_summary_ingested",
            "servers": len(server_ids),
            "inserted_servers": inserted,
            "already_ingested_servers": skipped,
            "summaries": summaries,
            "active_items": active_items,
            "transport_attempts": report["transport_attempts"],
            "source_sha256": source_sha256,
        }
    finally:
        connection.close()


def ingest_multi_scan(report_path: Path, database: Path) -> dict[str, Any]:
    raw = report_path.read_bytes()
    document = json.loads(raw.decode("utf-8"))
    report = document.get("report", document) if isinstance(document, dict) else None
    if not isinstance(report, dict) or report.get("format") != "aion2_market_multi_session_scan_v1":
        raise ValueError("market_history_report_invalid")
    try:
        from market_session_scan import validate as validate_multi_scan
    except ModuleNotFoundError:
        from scripts.market_session_scan import validate as validate_multi_scan
    validate_multi_scan(report)
    observed = dt.datetime.fromtimestamp(
        report_path.stat().st_mtime, tz=dt.timezone.utc
    ).isoformat().replace("+00:00", "Z")
    inserted = skipped = listings = targets = 0
    with tempfile.TemporaryDirectory(prefix="aion2-multi-scan-") as directory:
        root = Path(directory)
        for index, child in enumerate(report["responses"]):
            path = root / f"scope-{index:03d}.json"
            path.write_text(
                json.dumps({"report": child}, ensure_ascii=False, sort_keys=True,
                           separators=(",", ":")),
                encoding="utf-8",
            )
            server_id = int(child["request"]["game_server_id"])
            digest = hashlib.sha256(
                raw + b":" + str(server_id).encode("ascii")
            ).hexdigest()
            receipt = ingest(
                path,
                database,
                source_sha256=digest,
                observed_at=observed,
            )
            if receipt["status"] == "already_ingested":
                skipped += 1
            else:
                inserted += 1
                listings += receipt["listings"]
                targets += receipt["targets"]
    return {
        "status": "multi_scan_ingested",
        "servers": len(report["responses"]),
        "inserted_servers": inserted,
        "already_ingested_servers": skipped,
        "targets": targets,
        "listings": listings,
        "source_sha256": _sha256(raw),
    }


def _price_number(value: str) -> int | float:
    number = Decimal(value)
    if number == number.to_integral_value():
        return int(number)
    return float(number)


def _ui_data(database: Path) -> tuple[dict[str, Any], dict[int, list[dict[str, Any]]], dict[int, list[dict[str, Any]]]]:
    connection = _connect(database)
    connection.row_factory = sqlite3.Row
    try:
        active_server_row = connection.execute(
            """SELECT game_server_id FROM (
                 SELECT game_server_id,observed_at FROM summary_runs
                 UNION ALL SELECT game_server_id,observed_at FROM scans
               ) ORDER BY observed_at DESC LIMIT 1"""
        ).fetchone()
        active_server_id = int(active_server_row[0]) if active_server_row else None
        items = [dict(row) for row in connection.execute(
            """SELECT i.item_id,i.display_name,i.internal_name,i.item_type,i.item_level,
                      COALESCE(st.scan_status,'pending') AS scan_status,
                      COALESCE(st.next_page,1) AS next_page,st.last_scanned_at,
                      COALESCE(st.last_listing_count,0) AS last_listing_count,
                      COALESCE(st.scan_attempts,0) AS scan_attempts,st.last_error,
                      COALESCE(st.current_generation,0) AS current_generation,
                      count(l.sale_id) AS listing_count,min(l.sale_price) AS listing_min,
                      max(l.sale_price) AS listing_max,avg(l.sale_price) AS listing_avg
               FROM items i LEFT JOIN item_server_state st
                 ON st.game_server_id=? AND st.item_id=i.item_id
               LEFT JOIN scans ls ON ls.game_server_id=st.game_server_id
               LEFT JOIN listings l ON l.scan_id=ls.scan_id AND l.item_id=i.item_id
                 AND l.generation=st.current_generation
               GROUP BY i.item_id ORDER BY i.priority DESC,i.item_id"""
            , (active_server_id,)
        )]
        scans = [dict(row) for row in connection.execute(
            """SELECT scan_id,observed_at,game_server_id,listing_count,complete,
                      request_count,target_count,has_next_page
               FROM scans WHERE (? IS NULL OR game_server_id=?)
               ORDER BY observed_at DESC,scan_id DESC LIMIT 100""",
            (active_server_id, active_server_id),
        )]
        summary_runs = [dict(row) for row in connection.execute(
            """SELECT summary_id,observed_at,game_server_id,summary_count
               FROM summary_runs WHERE (? IS NULL OR game_server_id=?)
               ORDER BY observed_at DESC,summary_id DESC LIMIT 100""",
            (active_server_id, active_server_id),
        )]
        latest_summary_id = summary_runs[0]["summary_id"] if summary_runs else None
        current_summary_rows = [] if latest_summary_id is None else [
            dict(row) for row in connection.execute(
                """SELECT item_id,min_unit_price,sale_count,conditions_json
                   FROM summary_prices WHERE summary_id=? ORDER BY item_id""",
                (latest_summary_id,),
            )
        ]
        summary_history_rows = [dict(row) for row in connection.execute(
                """SELECT sp.item_id,sp.summary_id,sr.observed_at,
                      sp.min_unit_price,sp.sale_count
               FROM summary_prices sp JOIN summary_runs sr USING(summary_id)
               WHERE sr.game_server_id=? AND
                     sp.summary_id IN (
                  SELECT summary_id FROM summary_runs
                  WHERE game_server_id=?
                  ORDER BY observed_at DESC,summary_id DESC LIMIT 24
                )
               ORDER BY sp.item_id,sr.observed_at DESC,sp.summary_id DESC""",
            (active_server_id, active_server_id),
        )] if active_server_id is not None else []
        current_rows = [dict(row) for row in connection.execute(
            """SELECT l.*,s.observed_at,s.game_server_id FROM listings l
               JOIN scans s USING(scan_id)
               JOIN item_server_state st
                 ON st.game_server_id=s.game_server_id AND st.item_id=l.item_id
                AND st.current_generation=l.generation
               WHERE s.game_server_id=?
               ORDER BY l.item_id,l.sale_price,l.sale_id"""
            , (active_server_id,)
        )] if active_server_id is not None else []
        history_rows = [dict(row) for row in connection.execute(
            """SELECT si.item_id,si.scan_id,s.observed_at,si.status,si.page_count,
                      si.listing_count,min(l.sale_price) AS minimum,max(l.sale_price) AS maximum
               FROM scan_items si JOIN scans s USING(scan_id)
               LEFT JOIN listings l ON l.scan_id=si.scan_id AND l.item_id=si.item_id
               WHERE s.game_server_id=?
               GROUP BY si.scan_id,si.item_id ORDER BY si.item_id,s.observed_at DESC""",
            (active_server_id,),
        )] if active_server_id is not None else []
        catalog = connection.execute(
            """SELECT catalog_sha256,source_build,applies_to_build,table_version,
                      recovered_item_count,imported_at FROM catalogs
               ORDER BY imported_at DESC LIMIT 1"""
        ).fetchone()
        active_detail_items = 0
        if latest_summary_id is not None:
            active_detail_items = connection.execute(
                """WITH latest AS (
                     SELECT summary_id,observed_at,game_server_id
                     FROM summary_runs ORDER BY observed_at DESC,summary_id DESC LIMIT 1
                   ), active AS (
                     SELECT sp.item_id
                     FROM summary_prices sp,latest l
                     WHERE sp.summary_id=l.summary_id
                     GROUP BY sp.item_id HAVING sum(sp.sale_count)>0
                   ), covered AS (
                     SELECT DISTINCT si.item_id
                     FROM scan_items si JOIN scans s USING(scan_id),latest l
                     WHERE s.game_server_id=l.game_server_id
                       AND s.observed_at>=l.observed_at
                       AND si.status IN ('scanned_empty','scanned_listed')
                   )
                   SELECT count(*) FROM active JOIN covered USING(item_id)"""
            ).fetchone()[0]
    finally:
        connection.close()
    lots: dict[int, list[dict[str, Any]]] = {}
    for row in current_rows:
        lots.setdefault(row["item_id"], []).append(row)
    history: dict[int, list[dict[str, Any]]] = {}
    for row in history_rows:
        history.setdefault(row["item_id"], []).append(row)

    current_summary: dict[int, dict[str, Any]] = {}
    for row in current_summary_rows:
        item_id = row["item_id"]
        price = Decimal(row["min_unit_price"])
        aggregate = current_summary.setdefault(item_id, {
            "minimum": price, "sale_count": 0, "condition_variants": 0,
        })
        aggregate["minimum"] = min(aggregate["minimum"], price)
        aggregate["sale_count"] += row["sale_count"]
        aggregate["condition_variants"] += 1

    summary_history_groups: dict[tuple[int, int], dict[str, Any]] = {}
    for row in summary_history_rows:
        key = (row["item_id"], row["summary_id"])
        price = Decimal(row["min_unit_price"])
        aggregate = summary_history_groups.setdefault(key, {
            "item_id": row["item_id"], "summary_id": row["summary_id"],
            "observed_at": row["observed_at"], "minimum": price,
            "sale_count": 0,
        })
        aggregate["minimum"] = min(aggregate["minimum"], price)
        aggregate["sale_count"] += row["sale_count"]
    summary_history: dict[int, list[dict[str, Any]]] = {}
    for aggregate in summary_history_groups.values():
        item_id = aggregate["item_id"]
        minimum = _price_number(str(aggregate["minimum"]))
        summary_history.setdefault(item_id, []).append({
            "scan_id": f"summary-{aggregate['summary_id']}",
            "observed_at": aggregate["observed_at"],
            "status": "summary", "page_count": 0,
            "listing_count": aggregate["sale_count"],
            "minimum": minimum, "maximum": minimum,
        })
    for item_rows in summary_history.values():
        item_rows.sort(key=lambda row: row["observed_at"], reverse=True)

    counts = {state: 0 for state in SCAN_STATES}
    for item in items:
        counts[item["scan_status"]] = counts.get(item["scan_status"], 0) + 1
        aggregate = current_summary.get(item["item_id"])
        item["summary_min"] = (
            _price_number(str(aggregate["minimum"])) if aggregate else None
        )
        item["summary_sale_count"] = aggregate["sale_count"] if aggregate else 0
        item["summary_condition_variants"] = (
            aggregate["condition_variants"] if aggregate else 0
        )
        item["summary_observed_at"] = (
            summary_runs[0]["observed_at"] if aggregate and summary_runs else None
        )
        item["current_count"] = (
            item["summary_sale_count"] if aggregate else item["listing_count"]
        )
        item["current_min"] = (
            item["summary_min"] if aggregate else item["listing_min"]
        )
        item["current_max"] = item["listing_max"]
        item["current_avg"] = item["listing_avg"]
        item["detail_url"] = (
            f"items/{item['item_id']}.html"
            if item["current_count"] or item["listing_count"] else None
        )
        item["market_state"] = (
            "summary_active" if item["summary_sale_count"] > 0
            and item["scan_status"] != "scanned_listed" else item["scan_status"]
        )
        source_rows = summary_history.get(item["item_id"]) or history.get(item["item_id"], [])
        points = [
            {"at": row["observed_at"], "value": row["minimum"]}
            for row in reversed(source_rows) if row["minimum"] is not None
        ][-24:]
        item["price_history"] = points
        if len(points) > 1 and points[-2]["value"]:
            item["change_percent"] = round(
                (points[-1]["value"] - points[-2]["value"])
                * 100 / points[-2]["value"], 2
            )
        else:
            item["change_percent"] = None
        if item["item_id"] in summary_history:
            history[item["item_id"]] = (
                summary_history[item["item_id"]] + history.get(item["item_id"], [])
            )
    scanned = counts.get("scanned_empty", 0) + counts.get("scanned_listed", 0)
    total = len(items)
    summary_items = sum(
        1 for row in current_summary.values() if row["sale_count"] > 0
    )
    data = {
        "catalog": dict(catalog) if catalog else None,
        "stats": {
            "total": total,
            "scanned": scanned,
            "coverage_percent": round(scanned * 100 / total, 2) if total else 0,
            "summary_items": summary_items,
            "active_detail_items": active_detail_items,
            "active_detail_remaining": max(0, summary_items - active_detail_items),
            "active_detail_percent": (
                round(active_detail_items * 100 / summary_items, 2)
                if summary_items else 0
            ),
            "summary_sale_count": sum(row["sale_count"] for row in current_summary.values()),
            "summary_observed_at": summary_runs[0]["observed_at"] if summary_runs else None,
            "game_server_id": active_server_id,
            **counts,
        },
        "items": items,
        "scans": scans,
        "summary_runs": summary_runs,
    }
    return data, lots, history


def _json_for_html(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/")


def _detail_document(item: dict[str, Any], lots: list[dict[str, Any]], history_rows: list[dict[str, Any]]) -> str:
    title = html.escape(item["display_name"])
    payload = _json_for_html({"item": item, "lots": lots, "history": history_rows})
    return f"""<!doctype html><html lang="ru"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{title} · AION2 Market</title><style>
:root{{--bg:#080d16;--panel:#111a29;--line:#28364c;--text:#edf3fb;--muted:#94a3b8;--gold:#f5c75c}}
*{{box-sizing:border-box}}body{{margin:0;background:linear-gradient(145deg,#070b12,#101b2d);color:var(--text);font:14px/1.45 system-ui,sans-serif}}main{{max-width:1320px;margin:auto;padding:28px}}a{{color:#8cc9ff}}h1{{margin:.3em 0}}.meta{{color:var(--muted)}}.cards{{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:12px;margin:20px 0}}.card,.table{{background:rgba(17,26,41,.96);border:1px solid var(--line);border-radius:12px;padding:14px}}.card b{{display:block;font-size:22px;color:var(--gold)}}.table{{overflow:auto;padding:0;margin:14px 0}}table{{border-collapse:collapse;width:100%;white-space:nowrap}}th,td{{padding:10px 12px;border-bottom:1px solid var(--line);text-align:left}}th{{color:var(--muted);font-size:12px;text-transform:uppercase}}.price{{color:var(--gold);font-weight:700}}
</style></head><body><main><a href="../index.html">← Ко всему рынку</a><h1>{title}</h1><div class="meta">ID {item['item_id']} · {html.escape(item['internal_name'])} · {html.escape(item['item_type'])} · item level {item['item_level']}</div>
<div class="cards"><div class="card">Предложений в summary<b>{item['summary_sale_count']}</b></div><div class="card">Минимум<b>{item['current_min'] or '—'}</b></div><div class="card">Детальных лотов<b>{item['listing_count']}</b></div><div class="card">Статус<b>{html.escape(item['market_state'])}</b></div></div>
<h2>Детальные лоты</h2><div class="table"><table><thead><tr><th>Цена</th><th>Кол-во</th><th>За единицу</th><th>Истекает</th><th>Категория</th><th>Grade</th></tr></thead><tbody id="lots"></tbody></table></div>
<h2>История сканов</h2><div class="table"><table><thead><tr><th>Время</th><th>Статус</th><th>Страниц</th><th>Лотов</th><th>Мин.</th><th>Макс.</th></tr></thead><tbody id="history"></tbody></table></div>
</main><script id="market-data" type="application/json">{payload}</script><script>
const d=JSON.parse(document.getElementById('market-data').textContent);const e=s=>String(s??'—').replace(/[&<>"']/g,c=>({{'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}}[c]));
document.getElementById('lots').innerHTML=d.lots.map(r=>`<tr><td class="price">${{Number(r.sale_price).toLocaleString()}}</td><td>${{r.quantity}}</td><td>${{e(r.unit_price)}}</td><td>${{e(r.effective_to)}}</td><td>${{r.category_id}}</td><td>${{r.grade_id}}</td></tr>`).join('');
document.getElementById('history').innerHTML=d.history.map(r=>`<tr><td>${{e(r.observed_at)}}</td><td>${{e(r.status)}}</td><td>${{r.page_count}}</td><td>${{r.listing_count}}</td><td class="price">${{r.minimum==null?'—':Number(r.minimum).toLocaleString()}}</td><td>${{r.maximum==null?'—':Number(r.maximum).toLocaleString()}}</td></tr>`).join('');
</script></body></html>"""


def render(database: Path, output: Path) -> dict[str, Any]:
    data, lots, history_rows = _ui_data(database)
    payload = _json_for_html(data)
    stats = data["stats"]
    warning = ""
    if not data["items"]:
        warning = '<section class="notice" data-state="catalog-empty"><strong>Каталог не импортирован</strong><p>Импортируйте client-derived item catalog.</p><button class="copy-llm">Скопировать для LLM</button></section>'
    elif stats["summary_observed_at"] and stats["active_detail_remaining"]:
        warning = f'<section class="notice" data-state="details-incomplete"><strong>Summary рынка получен, детальные лоты ещё догружаются</strong><p>Summary нашёл {stats["summary_items"]} активных item ID; в текущем server/summary окне детально разобрано {stats["active_detail_items"]}. Очередь listing-запросов строится только по активным позициям.</p><button class="copy-llm">Скопировать для LLM</button></section>'
    elif not stats["summary_observed_at"] and stats["scanned"] < stats["total"]:
        warning = f'<section class="notice" data-state="coverage-incomplete"><strong>Обход рынка не завершён</strong><p>Покрыто {stats["scanned"]} из {stats["total"]} item ID ({stats["coverage_percent"]}%). Следующий bounded batch продолжит checkpoint.</p><button class="copy-llm">Скопировать для LLM</button></section>'
    document = f"""<!doctype html>
<html lang="ru"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>AION2 Economy</title><style>
:root{{--bg:#090c0f;--sidebar:#11161b;--panel:#151b21;--panel2:#1b2229;--line:#2a333c;--text:#e7edf2;--muted:#87939e;--gold:#d9b35f;--blue:#6ba6cc;--green:#54c59c;--red:#df6a75;--orange:#c98b52}}
*{{box-sizing:border-box}}html{{background:var(--bg)}}body{{margin:0;color:var(--text);font:13px/1.45 Inter,Segoe UI,system-ui,sans-serif;background:radial-gradient(circle at 70% -30%,#26323a 0,transparent 42rem),var(--bg)}}button,input,select{{font:inherit}}.topbar{{height:54px;border-bottom:1px solid var(--line);background:#0d1115dd;backdrop-filter:blur(12px);display:flex;align-items:center;padding:0 20px;position:sticky;top:0;z-index:20}}.brand{{font-size:18px;font-weight:750;letter-spacing:.02em}}.brand em{{font-style:normal;color:var(--gold)}}.realm{{margin-left:auto;color:var(--muted);border:1px solid var(--line);background:var(--panel);padding:6px 10px;border-radius:4px}}
.layout{{max-width:1640px;margin:auto;display:grid;grid-template-columns:238px minmax(0,1fr);min-height:calc(100vh - 54px)}}aside{{border-right:1px solid var(--line);padding:22px 14px;background:linear-gradient(180deg,#11161bdd,#0d1115)}}.side-title{{color:#66727d;text-transform:uppercase;font-weight:700;font-size:11px;letter-spacing:.1em;padding:0 10px;margin:8px 0}}.nav{{display:grid;gap:3px;margin-bottom:24px}}.nav button{{display:flex;align-items:center;gap:10px;width:100%;border:0;background:transparent;color:#aeb9c2;text-align:left;padding:9px 10px;border-radius:4px;cursor:pointer}}.nav button:hover{{background:#1a2229;color:white}}.nav button.active{{background:#28333c;color:white;box-shadow:inset 2px 0 var(--gold)}}.nav .count{{margin-left:auto;color:#65717c;font-variant-numeric:tabular-nums}}.coverage-box{{margin:20px 8px;padding:13px;background:#0c1115;border:1px solid var(--line);border-radius:5px}}.coverage-head{{display:flex;justify-content:space-between;color:var(--muted)}}.meter{{height:5px;background:#222a31;margin:10px 0;border-radius:9px;overflow:hidden}}.meter i{{display:block;height:100%;background:var(--gold);width:{stats['active_detail_percent']}%}}
main{{min-width:0;padding:28px 32px 60px}}.crumb{{font-size:12px;color:#65717c;text-transform:uppercase;letter-spacing:.08em}}h1{{font-size:27px;margin:7px 0 3px}}.subtitle{{color:var(--muted)}}.notice{{margin:18px 0;padding:13px 15px;background:#2b1d1c;border:1px solid #67433d;border-radius:5px;display:flex;gap:12px;align-items:center;flex-wrap:wrap}}.notice p{{margin:0;flex:1;min-width:280px}}.notice button,.pager button{{background:#202830;border:1px solid #37434d;color:var(--text);padding:7px 10px;border-radius:4px;cursor:pointer}}
.market-stats{{display:grid;grid-template-columns:repeat(4,minmax(140px,1fr));margin:22px 0;border:1px solid var(--line);background:var(--panel);border-radius:5px;overflow:hidden}}.metric{{padding:14px 17px;border-right:1px solid var(--line)}}.metric:last-child{{border:0}}.metric span{{display:block;color:var(--muted);font-size:11px;text-transform:uppercase;letter-spacing:.07em}}.metric b{{font-size:21px;font-weight:650;font-variant-numeric:tabular-nums}}
.controls{{display:grid;grid-template-columns:minmax(240px,1fr) repeat(2,180px);gap:8px;margin:14px 0}}input,select{{width:100%;background:#10161b;border:1px solid var(--line);color:var(--text);border-radius:4px;padding:9px 10px;outline:none}}input:focus,select:focus{{border-color:#5d7f96}}.table-wrap{{overflow:auto;border:1px solid var(--line);border-radius:5px;background:#10151a}}table{{width:100%;border-collapse:collapse;white-space:nowrap}}th{{position:sticky;top:0;background:#171d23;color:#7f8b96;font-size:11px;text-transform:uppercase;letter-spacing:.06em;text-align:right;padding:10px 12px;border-bottom:1px solid var(--line)}}th:nth-child(-n+2){{text-align:left}}td{{padding:9px 12px;border-bottom:1px solid #222a31;text-align:right;font-variant-numeric:tabular-nums}}td:nth-child(-n+2){{text-align:left}}tbody tr:hover{{background:#192128}}tr[data-url]{{cursor:pointer}}.item-cell{{display:flex;align-items:center;gap:11px}}.item-icon{{width:38px;height:38px;display:grid;place-items:center;border-radius:4px;background:linear-gradient(145deg,#38434b,#1b2228);border:1px solid #46525d;color:var(--gold);font-weight:800}}.item-name strong{{display:block;color:#e8edf1;font-size:13px}}.item-name small{{display:block;color:#687680;max-width:420px;overflow:hidden;text-overflow:ellipsis}}.price{{font-weight:700;color:var(--gold);font-size:14px}}.subprice{{display:block;color:#687680;font-size:11px}}.change.up{{color:var(--green)}}.change.down{{color:var(--red)}}.state{{display:inline-block;padding:3px 6px;border-radius:3px;background:#25313a;color:#a9b7c1;font-size:11px}}.state.scanned_listed{{background:#18352d;color:#72d2b1}}.state.partial{{background:#3b3020;color:#e5b96b}}.spark{{width:110px;height:30px}}.spark polyline{{fill:none;stroke:var(--blue);stroke-width:1.8}}.spark .area{{fill:#6ba6cc18;stroke:none}}.empty{{color:#58636c}}.pager{{display:flex;align-items:center;justify-content:flex-end;gap:12px;padding:12px 2px;color:var(--muted)}}.runs{{margin-top:28px}}.runs summary{{cursor:pointer;color:#89959f;padding:8px 0}}
@media(max-width:980px){{.layout{{grid-template-columns:1fr}}aside{{border-right:0;border-bottom:1px solid var(--line);padding:10px}}.nav{{grid-template-columns:repeat(4,1fr);margin:0}}.nav button{{justify-content:center}}.side-title,.coverage-box{{display:none}}main{{padding:20px 14px}}.market-stats{{grid-template-columns:1fr 1fr}}.metric:nth-child(2){{border-right:0}}.controls{{grid-template-columns:1fr 1fr}}.controls input{{grid-column:1/-1}}}}
</style></head><body><header class="topbar"><div class="brand">AION2 <em>economy</em></div><div class="realm" id="realm">Global · build —</div></header><div class="layout"><aside><div class="side-title">Экономика</div><nav class="nav" id="nav"><button class="active" data-view="overview">Обзор рынка<span class="count" data-count="listed">0</span></button><button data-view="all">Все предметы<span class="count" data-count="all">0</span></button><button data-view="Equip">Экипировка<span class="count" data-count="Equip">0</span></button><button data-view="Usable">Расходники<span class="count" data-count="Usable">0</span></button><button data-view="Misc">Прочее<span class="count" data-count="Misc">0</span></button><button data-view="empty">Без лотов<span class="count" data-count="empty">0</span></button><button data-view="pending">Не проверено<span class="count" data-count="pending">0</span></button></nav><div class="coverage-box"><div class="coverage-head"><span>Активный рынок</span><b>{stats['active_detail_percent']}%</b></div><div class="meter"><i></i></div><small class="subtitle">{stats['active_detail_items']} / {stats['summary_items']} item ID</small></div></aside>
<main><div class="crumb">Global Market / Economy</div><h1 id="view-title">Обзор рынка</h1><div class="subtitle">Whole-market summary для discovery, детальные listings для лотов и истории.</div>{warning}
<section class="market-stats"><div class="metric"><span>Активных item ID</span><b id="m-items">0</b></div><div class="metric"><span>Предложений в summary</span><b id="m-lots">0</b></div><div class="metric"><span>Активных детализировано</span><b>{stats['active_detail_items']}</b></div><div class="metric"><span>Всего в client catalog</span><b>{stats['total']}</b></div></section>
<div class="controls"><input id="search" placeholder="Поиск по названию, internal name или item ID"><select id="state"><option value="">Все состояния</option><option value="scanned_listed">Есть лоты</option><option value="scanned_empty">Сканировано, пусто</option><option value="partial">Checkpoint</option><option value="pending">Не сканировалось</option><option value="error">Ошибка</option></select><select id="sort"><option value="price">Цена: сначала дешёвые</option><option value="lots">Больше лотов</option><option value="change">Изменение цены</option><option value="name">Название</option><option value="id">Item ID</option></select></div>
<div class="table-wrap"><table><thead><tr><th>#</th><th>Предмет</th><th>Мин. цена</th><th>Средняя</th><th>Лотов</th><th>Изменение</th><th>Тренд</th><th>Состояние</th><th>Обновлено</th></tr></thead><tbody id="items"></tbody></table></div><div class="pager"><button id="prev">←</button><span id="summary"></span><button id="next">→</button></div>
<details class="runs"><summary>Whole-market summary snapshots</summary><div class="table-wrap"><table><thead><tr><th>Summary</th><th>Время</th><th>Server</th><th>Строк</th></tr></thead><tbody id="summary-run-rows"></tbody></table></div></details>
<details class="runs"><summary>Последние детальные listing scans</summary><div class="table-wrap"><table><thead><tr><th>Scan</th><th>Время</th><th>Server</th><th>Targets</th><th>Requests</th><th>Лоты</th><th>Результат</th></tr></thead><tbody id="run-rows"></tbody></table></div></details>
</main></div><script id="market-data" type="application/json">{payload}</script><script>
const d=JSON.parse(document.getElementById('market-data').textContent),size=60;let page=0,view='overview';const $=id=>document.getElementById(id);const esc=s=>String(s??'—').replace(/[&<>"']/g,c=>({{'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}}[c]));const money=v=>v==null?'—':Number(v).toLocaleString('ru-RU');const labels={{overview:'Обзор рынка',all:'Все предметы',Equip:'Экипировка',Usable:'Расходники',Misc:'Прочее',empty:'Без активных лотов',pending:'Не проверено'}};
function spark(points){{const values=points.map(p=>p.value);if(values.length<2)return '<span class="empty">—</span>';const min=Math.min(...values),max=Math.max(...values),range=max-min||1,xy=values.map((v,i)=>`${{(i/(values.length-1)*108+1).toFixed(1)}},${{(28-(v-min)/range*25).toFixed(1)}}`).join(' ');return `<svg class="spark" viewBox="0 0 110 30" preserveAspectRatio="none"><polyline points="${{xy}}"/></svg>`}}
function inView(r){{if(view==='overview')return r.summary_sale_count>0||r.scan_status==='scanned_listed';if(view==='all')return true;if(view==='empty')return r.scan_status==='scanned_empty'&&r.summary_sale_count===0;if(view==='pending')return ['pending','partial','error'].includes(r.scan_status);return r.item_type===view}}
function filtered(){{const q=$('search').value.trim().toLowerCase(),state=$('state').value,sort=$('sort').value;const rows=d.items.filter(r=>inView(r)&&(!q||String(r.item_id).includes(q)||r.display_name.toLowerCase().includes(q)||r.internal_name.toLowerCase().includes(q))&&(!state||r.scan_status===state));rows.sort((a,b)=>sort==='id'?a.item_id-b.item_id:sort==='price'?(a.current_min??Infinity)-(b.current_min??Infinity):sort==='lots'?b.current_count-a.current_count:sort==='change'?Math.abs(b.change_percent??-Infinity)-Math.abs(a.change_percent??-Infinity):a.display_name.localeCompare(b.display_name));return rows}}
function draw(){{const rows=filtered(),pages=Math.max(1,Math.ceil(rows.length/size));page=Math.min(page,pages-1);$('items').innerHTML=rows.slice(page*size,(page+1)*size).map((r,i)=>{{const ch=r.change_percent,cc=ch==null?'empty':ch>0?'up':ch<0?'down':'';return `<tr ${{r.detail_url?`data-url="${{esc(r.detail_url)}}"`:''}}><td>${{page*size+i+1}}</td><td><div class="item-cell"><span class="item-icon">${{esc(r.item_type[0])}}</span><span class="item-name"><strong>${{esc(r.display_name)}}</strong><small>${{r.item_id}} · ${{esc(r.internal_name)}} · ilvl ${{r.item_level}}</small></span></div></td><td class="price">${{money(r.current_min)}}<span class="subprice">Kina</span></td><td>${{r.current_avg==null?'—':money(Math.round(r.current_avg))}}</td><td>${{money(r.current_count)}}</td><td class="change ${{cc}}">${{ch==null?'—':`${{ch>0?'+':''}}${{ch.toFixed(2)}}%`}}</td><td>${{spark(r.price_history)}}</td><td><span class="state ${{esc(r.market_state)}}">${{esc(r.market_state)}}</span></td><td>${{r.summary_observed_at?new Date(r.summary_observed_at).toLocaleString('ru-RU'):(r.last_scanned_at?new Date(r.last_scanned_at).toLocaleString('ru-RU'):'—')}}</td></tr>`}}).join('');$('summary').textContent=`${{rows.length.toLocaleString('ru-RU')}} результатов · ${{page+1}} / ${{pages}}`;$('prev').disabled=page===0;$('next').disabled=page>=pages-1;document.querySelectorAll('tr[data-url]').forEach(r=>r.onclick=()=>location.href=r.dataset.url)}}
const counts={{all:d.items.length,listed:d.items.filter(r=>r.summary_sale_count>0||r.scan_status==='scanned_listed').length,empty:d.items.filter(r=>r.scan_status==='scanned_empty'&&r.summary_sale_count===0).length,pending:d.items.filter(r=>['pending','partial','error'].includes(r.scan_status)).length,Equip:d.items.filter(r=>r.item_type==='Equip').length,Usable:d.items.filter(r=>r.item_type==='Usable').length,Misc:d.items.filter(r=>r.item_type==='Misc').length}};document.querySelectorAll('[data-count]').forEach(e=>e.textContent=counts[e.dataset.count].toLocaleString('ru-RU'));$('m-items').textContent=(d.stats.summary_items||counts.listed).toLocaleString('ru-RU');$('m-lots').textContent=(d.stats.summary_sale_count||d.items.reduce((s,r)=>s+r.current_count,0)).toLocaleString('ru-RU');if(d.catalog)$('realm').textContent=`Global · build ${{d.catalog.applies_to_build}}`;
document.querySelectorAll('#nav button').forEach(b=>b.onclick=()=>{{document.querySelector('#nav .active').classList.remove('active');b.classList.add('active');view=b.dataset.view;$('view-title').textContent=labels[view];page=0;draw()}});['search','state','sort'].forEach(id=>$(id).addEventListener(id==='search'?'input':'change',()=>{{page=0;draw()}}));$('prev').onclick=()=>{{page--;draw()}};$('next').onclick=()=>{{page++;draw()}};draw();
$('summary-run-rows').innerHTML=d.summary_runs.map(r=>`<tr><td>#${{r.summary_id}}</td><td>${{esc(r.observed_at)}}</td><td>${{r.game_server_id}}</td><td>${{r.summary_count}}</td></tr>`).join('');$('run-rows').innerHTML=d.scans.map(r=>`<tr><td>#${{r.scan_id}}</td><td>${{esc(r.observed_at)}}</td><td>${{r.game_server_id}}</td><td>${{r.target_count}}</td><td>${{r.request_count}}</td><td>${{r.listing_count}}</td><td>${{r.complete?'complete':'checkpoint'}}</td></tr>`).join('');document.querySelectorAll('.copy-llm').forEach(button=>button.addEventListener('click',async event=>{{event.stopPropagation();const box=button.closest('.notice');const text=[box.innerText.replace('Скопировать для LLM','').trim(),`severity=warning state=${{box.dataset.state}}`,`catalog_items=${{d.stats.total}} scanned=${{d.stats.scanned}} summary_items=${{d.stats.summary_items}} active_detail_items=${{d.stats.active_detail_items}} active_detail_remaining=${{d.stats.active_detail_remaining}} summary_sale_count=${{d.stats.summary_sale_count}}`,`url=${{location.href}}`].join('\\n');try{{await navigator.clipboard.writeText(text);button.textContent='Скопировано'}}catch{{button.textContent='Не удалось скопировать'}}}}));
</script></body></html>"""
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(document, encoding="utf-8", newline="\n")
    detail_root = output.parent / "items"
    detail_root.mkdir(parents=True, exist_ok=True)
    detail_items = [item for item in data["items"] if item["detail_url"]]
    for item in detail_items:
        item_id = item["item_id"]
        (detail_root / f"{item_id}.html").write_text(
            _detail_document(item, lots.get(item_id, []), history_rows.get(item_id, [])),
            encoding="utf-8", newline="\n",
        )
    return {
        "status": "ui_rendered",
        "catalog_items": stats["total"],
        "scanned_items": stats["scanned"],
        "coverage_percent": stats["coverage_percent"],
        "current_listings": sum(len(rows) for rows in lots.values()),
        "summary_items": stats["summary_items"],
        "active_detail_items": stats["active_detail_items"],
        "active_detail_remaining": stats["active_detail_remaining"],
        "summary_sale_count": stats["summary_sale_count"],
        "detail_pages": len(detail_items),
        "scan_runs": len(data["scans"]),
        "summary_runs": len(data["summary_runs"]),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)
    catalog_parser = subparsers.add_parser("catalog")
    catalog_parser.add_argument("--catalog", type=Path, required=True)
    catalog_parser.add_argument("--db", type=Path, required=True)
    catalog_parser.add_argument("--source-build", type=int, required=True)
    catalog_parser.add_argument("--applies-to-build", type=int, required=True)
    plan_parser = subparsers.add_parser("plan")
    plan_parser.add_argument("--db", type=Path, required=True)
    plan_parser.add_argument("--out", type=Path, required=True)
    plan_parser.add_argument("--limit", type=int, default=2000)
    plan_parser.add_argument("--game-server-id", type=int)
    plan_parser.add_argument("--refresh-before")
    plan_parser.add_argument(
        "--mode", choices=("pending", "refresh", "summary-active"),
        default="pending",
    )
    ingest_parser = subparsers.add_parser("ingest")
    ingest_parser.add_argument("--report", type=Path, required=True)
    ingest_parser.add_argument("--db", type=Path, required=True)
    render_parser = subparsers.add_parser("render")
    render_parser.add_argument("--db", type=Path, required=True)
    render_parser.add_argument("--out", type=Path, required=True)
    build_parser = subparsers.add_parser("build")
    build_parser.add_argument("--report", type=Path, required=True)
    build_parser.add_argument("--db", type=Path, required=True)
    build_parser.add_argument("--out", type=Path, required=True)
    summary_ingest_parser = subparsers.add_parser("summary-ingest")
    summary_ingest_parser.add_argument("--report", type=Path, required=True)
    summary_ingest_parser.add_argument("--db", type=Path, required=True)
    summary_build_parser = subparsers.add_parser("summary-build")
    summary_build_parser.add_argument("--report", type=Path, required=True)
    summary_build_parser.add_argument("--db", type=Path, required=True)
    summary_build_parser.add_argument("--out", type=Path, required=True)
    multi_summary_ingest_parser = subparsers.add_parser("multi-summary-ingest")
    multi_summary_ingest_parser.add_argument("--report", type=Path, required=True)
    multi_summary_ingest_parser.add_argument("--db", type=Path, required=True)
    multi_summary_ingest_parser.add_argument("--expected-sha256")
    multi_summary_ingest_parser.add_argument("--observed-at", required=True)
    multi_summary_build_parser = subparsers.add_parser("multi-summary-build")
    multi_summary_build_parser.add_argument("--report", type=Path, required=True)
    multi_summary_build_parser.add_argument("--db", type=Path, required=True)
    multi_summary_build_parser.add_argument("--out", type=Path, required=True)
    multi_ingest_parser = subparsers.add_parser("multi-ingest")
    multi_ingest_parser.add_argument("--report", type=Path, required=True)
    multi_ingest_parser.add_argument("--db", type=Path, required=True)
    multi_build_parser = subparsers.add_parser("multi-build")
    multi_build_parser.add_argument("--report", type=Path, required=True)
    multi_build_parser.add_argument("--db", type=Path, required=True)
    multi_build_parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    if args.command == "catalog":
        result = import_catalog(
            args.catalog, args.db, args.source_build, args.applies_to_build
        )
    elif args.command == "plan":
        result = plan_batch(
            args.db, args.out, args.limit, args.mode, args.game_server_id,
            args.refresh_before,
        )
    elif args.command == "ingest":
        result = ingest(args.report, args.db)
    elif args.command == "render":
        result = render(args.db, args.out)
    elif args.command == "build":
        result = {
            "ingest": ingest(args.report, args.db),
            "render": render(args.db, args.out),
        }
    elif args.command == "summary-ingest":
        result = ingest_summary(args.report, args.db)
    elif args.command == "summary-build":
        result = {
            "ingest": ingest_summary(args.report, args.db),
            "render": render(args.db, args.out),
        }
    elif args.command == "multi-summary-ingest":
        result = ingest_multi_summary(
            args.report,
            args.db,
            observed_at=args.observed_at,
            expected_sha256=args.expected_sha256,
        )
    elif args.command == "multi-summary-build":
        result = {
            "ingest": ingest_multi_summary(
                args.report,
                args.db,
                observed_at=dt.datetime.now(dt.timezone.utc).isoformat(),
            ),
            "render": render(args.db, args.out),
        }
    elif args.command == "multi-ingest":
        result = ingest_multi_scan(args.report, args.db)
    else:
        result = {
            "ingest": ingest_multi_scan(args.report, args.db),
            "render": render(args.db, args.out),
        }
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
