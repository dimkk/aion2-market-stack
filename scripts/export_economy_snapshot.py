#!/usr/bin/env python3
"""Export the derived market database into the protected Economy API snapshot.

The result is intended for private Object Storage/API delivery.  It contains no
credentials, captures, request payloads, or session material.
"""

from __future__ import annotations

import argparse
from hashlib import sha256
import json
import math
import re
import sqlite3
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from statistics import median
from typing import Any, Iterable


SCHEMA_VERSION = "aion2.economy.snapshot/v1"
CRAFT_SCHEMA_VERSION = "aion2.craft-recipes/v1"
MARKET_RULES_SCHEMA_VERSION = "aion2.market-rules/v1"
MARKET_DIMENSIONS_SCHEMA_VERSION = "aion2.market-dimensions/v1"
ITEM_ICONS_SCHEMA_VERSION = "aion2.item-icons/v1"


def _timestamp(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)


def _number(value: str | int | float | Decimal | None) -> int | float | None:
    if value is None:
        return None
    try:
        number = Decimal(str(value))
    except InvalidOperation:
        return None
    if number == number.to_integral_value():
        return int(number)
    return float(number)


def _liquidity_score(sale_count: int, max_sales: int) -> float:
    if sale_count <= 0 or max_sales <= 0:
        return 0.0
    return min(1.0, math.log1p(sale_count) / math.log1p(max_sales))


@dataclass(frozen=True)
class Point:
    at: datetime
    value: int | float
    listings: int

    def json(self) -> dict[str, Any]:
        return {
            "at": self.at.isoformat().replace("+00:00", "Z"),
            "value": self.value,
            "listings": self.listings,
        }


def _change(points: list[Point], horizon: timedelta) -> float | None:
    if len(points) < 2:
        return None
    current = points[-1]
    cutoff = current.at - horizon
    candidates = [point for point in points[:-1] if point.at <= cutoff]
    if not candidates or candidates[-1].value == 0:
        return None
    reference = candidates[-1]
    # A named horizon must not silently use an arbitrarily old observation.
    tolerance = min(horizon / 4, timedelta(hours=24))
    if cutoff - reference.at > tolerance:
        return None
    return round((current.value - reference.value) * 100 / reference.value, 2)


def _deduplicate_points(points: Iterable[Point]) -> list[Point]:
    by_time: dict[datetime, Point] = {}
    for point in points:
        existing = by_time.get(point.at)
        if existing is None or point.listings >= existing.listings:
            by_time[point.at] = point
    return sorted(by_time.values(), key=lambda point: point.at)


def _history(connection: sqlite3.Connection) -> dict[tuple[int, int], list[Point]]:
    grouped: dict[tuple[int, int], list[Point]] = defaultdict(list)
    for row in connection.execute(
        """SELECT sr.game_server_id,sp.item_id,sr.observed_at,
                  min(CAST(sp.min_unit_price AS REAL)),sum(sp.sale_count)
           FROM summary_prices sp JOIN summary_runs sr USING(summary_id)
           GROUP BY sr.summary_id,sr.game_server_id,sp.item_id"""
    ):
        value = _number(row[3])
        if value is not None:
            grouped[(int(row[0]), int(row[1]))].append(
                Point(_timestamp(row[2]), value, int(row[4]))
            )
    for row in connection.execute(
        """SELECT s.game_server_id,l.item_id,s.observed_at,
                  min(CAST(l.unit_price AS REAL)),count(*)
           FROM listings l JOIN scans s USING(scan_id)
           GROUP BY s.scan_id,s.game_server_id,l.item_id"""
    ):
        value = _number(row[3])
        if value is not None:
            grouped[(int(row[0]), int(row[1]))].append(
                Point(_timestamp(row[2]), value, int(row[4]))
            )
    return {key: _deduplicate_points(points)[-168:] for key, points in grouped.items()}


def _latest_summary(connection: sqlite3.Connection) -> tuple[int | None, str | None, int | None]:
    row = connection.execute(
        """SELECT summary_id,observed_at,game_server_id
           FROM summary_runs ORDER BY observed_at DESC,summary_id DESC LIMIT 1"""
    ).fetchone()
    return (None, None, None) if row is None else (int(row[0]), str(row[1]), int(row[2]))


def _revision(build: int | None, generated_at: str, content_digest: str) -> str:
    normalized = _timestamp(generated_at).strftime("%Y%m%dT%H%M%SZ")
    return f"b{build or 0}-{normalized}-c{content_digest[:12]}"


def _recipe_names(
    item_id: int,
    source_name: Any,
    source_names: Any,
    catalog_names: dict[int, dict[str, str]],
) -> dict[str, str]:
    catalog = catalog_names.get(item_id, {})
    supplied = source_names if isinstance(source_names, dict) else {}
    fallback = source_name.strip() if isinstance(source_name, str) else ""
    english = supplied.get("en") if isinstance(supplied.get("en"), str) else ""
    russian = supplied.get("ru") if isinstance(supplied.get("ru"), str) else ""
    english = english.strip() or catalog.get("en", "").strip() or fallback or f"Item {item_id}"
    russian = russian.strip() or catalog.get("ru", "").strip() or english
    return {"en": english, "ru": russian}


def _load_recipes(
    path: Path | None,
    build: int | None,
    catalog_names: dict[int, dict[str, str]] | None = None,
) -> list[dict[str, Any]]:
    if path is None:
        return []
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict) or value.get("schemaVersion") != CRAFT_SCHEMA_VERSION:
        raise ValueError("craft_recipe_schema_mismatch")
    applies_to_build = value.get("appliesToBuild")
    if not isinstance(applies_to_build, int) or isinstance(applies_to_build, bool):
        raise ValueError("craft_recipe_build_missing")
    if build is not None and applies_to_build != build:
        raise ValueError("craft_recipe_build_mismatch")
    rows = value.get("recipes")
    if not isinstance(rows, list) or len(rows) > 10_000:
        raise ValueError("craft_recipe_rows_invalid")

    catalog_names = catalog_names or {}
    projected: list[dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, dict) or row.get("showRecipe") is not True:
            continue
        output = row.get("output")
        materials = row.get("materials")
        if not isinstance(output, dict) or not isinstance(materials, list) or not materials:
            raise ValueError("craft_recipe_visible_row_invalid")
        recipe_id = row.get("recipeId")
        output_item_id = output.get("itemId")
        output_quantity = output.get("quantity")
        fee = row.get("fee")
        name = row.get("name")
        if (
            not isinstance(recipe_id, int)
            or isinstance(recipe_id, bool)
            or not isinstance(output_item_id, int)
            or isinstance(output_item_id, bool)
            or not isinstance(output_quantity, int)
            or isinstance(output_quantity, bool)
            or output_quantity <= 0
            or not isinstance(fee, int)
            or isinstance(fee, bool)
            or fee < 0
            or not isinstance(name, str)
            or not name
        ):
            raise ValueError("craft_recipe_visible_row_invalid")
        output_names = _recipe_names(
            output_item_id, output.get("name"), output.get("names"), catalog_names
        )
        projected_materials: list[dict[str, Any]] = []
        for material in materials:
            if not isinstance(material, dict):
                raise ValueError("craft_recipe_material_invalid")
            item_id = material.get("itemId")
            quantity = material.get("quantity")
            if (
                not isinstance(item_id, int)
                or isinstance(item_id, bool)
                or not isinstance(quantity, int)
                or isinstance(quantity, bool)
                or quantity <= 0
            ):
                raise ValueError("craft_recipe_material_invalid")
            material_names = _recipe_names(
                item_id, material.get("name"), material.get("names"), catalog_names
            )
            projected_materials.append({
                "itemId": item_id,
                "quantity": quantity,
                "name": material_names["en"],
                "names": material_names,
            })
        category = row.get("category")
        subcategory = row.get("subcategory")
        subtab = row.get("subtab")
        projected.append({
            "recipeId": recipe_id,
            "name": output_names["en"] if name == output.get("name") else name,
            "names": output_names,
            "category": category.strip() if isinstance(category, str) else "",
            "subcategory": subcategory.strip() if isinstance(subcategory, str) else "",
            "subtab": subtab.strip() if isinstance(subtab, str) else "",
            "outputItemId": output_item_id,
            "outputName": output_names["en"],
            "outputNames": output_names,
            "outputQuantity": output_quantity,
            "fee": fee,
            "materials": projected_materials,
        })
    return projected


def _load_market_rules(path: Path | None, build: int | None) -> dict[str, Any]:
    unavailable = {"saleTaxRate": None, "saleTaxSource": None, "serverRates": []}
    if path is None:
        return unavailable
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict) or set(value) != {
        "schemaVersion", "appliesToBuild", "observedAt", "source", "serverFees"
    } or value.get("schemaVersion") != MARKET_RULES_SCHEMA_VERSION:
        raise ValueError("market_rules_schema_mismatch")
    if value.get("appliesToBuild") != build:
        raise ValueError("market_rules_build_mismatch")
    source = value.get("source")
    if not isinstance(source, dict) or set(source) != {
        "authority", "path", "captureSha256", "gameServerIds"
    } or source.get("authority") != "qos-api.g.nc.com" or source.get("path") != "/trade/v1.0/policies":
        raise ValueError("market_rules_source_invalid")
    if not re.fullmatch(r"[0-9a-f]{64}", str(source.get("captureSha256", ""))):
        raise ValueError("market_rules_source_invalid")
    server_ids = source.get("gameServerIds")
    if not isinstance(server_ids, list) or not server_ids or any(
        type(server_id) is not int or server_id <= 0 for server_id in server_ids
    ):
        raise ValueError("market_rules_source_invalid")
    try:
        _timestamp(value.get("observedAt"))
    except (AttributeError, TypeError, ValueError) as error:
        raise ValueError("market_rules_source_invalid") from error
    server_fees = value.get("serverFees")
    if not isinstance(server_fees, list) or not server_fees:
        raise ValueError("market_rules_fee_invalid")
    proportional = {"RatioSellerCommission", "RatioRegistrationFee"}
    results: list[dict[str, Any]] = []
    seen_servers: set[int] = set()
    for server_fee in server_fees:
        if not isinstance(server_fee, dict) or set(server_fee) != {"gameServerId", "fees"}:
            raise ValueError("market_rules_fee_invalid")
        server_id, fees = server_fee.get("gameServerId"), server_fee.get("fees")
        if type(server_id) is not int or server_id not in server_ids or server_id in seen_servers or not isinstance(fees, list):
            raise ValueError("market_rules_fee_invalid")
        seen_servers.add(server_id)
        rates: dict[str, Decimal] = {}
        for fee in fees:
            if not isinstance(fee, dict) or set(fee) != {"feeType", "rawValue"}:
                raise ValueError("market_rules_fee_invalid")
            fee_type, rate = fee.get("feeType"), fee.get("rawValue")
            if fee_type not in proportional or fee_type in rates or isinstance(rate, bool):
                raise ValueError("market_rules_fee_invalid")
            try:
                parsed = Decimal(str(rate))
            except InvalidOperation as error:
                raise ValueError("market_rules_fee_invalid") from error
            if not Decimal("0") <= parsed <= Decimal("100"):
                raise ValueError("market_rules_fee_invalid")
            rates[fee_type] = parsed
        if set(rates) != proportional:
            raise ValueError("market_rules_fee_invalid")
        results.append({
            "serverId": server_id,
            # Profit uses only the explicitly named seller commission. The
            # registration policy value has not yet been proven to share the
            # same percentage base and is therefore retained only in the
            # source evidence, never silently added to sale tax.
            "saleTaxRate": float(rates["RatioSellerCommission"] / Decimal("100")),
            "source": (
                f"first-party seller commission from {source['authority']}"
                f"{source['path']} observed {value['observedAt']}"
            ),
        })
    return {
        "saleTaxRate": None,
        "saleTaxSource": "server-specific first-party market policy",
        "serverRates": sorted(results, key=lambda row: row["serverId"]),
    }


def _load_market_dimensions(
    path: Path | None, build: int | None,
) -> tuple[dict[int, dict[str, Any]], str | None]:
    """Load only explicitly evidenced labels for backend market server IDs.

    The exporter deliberately has no built-in region, server-name, faction, or
    cross-server mapping.  Missing rows therefore remain unconfirmed instead
    of inheriting a guess from another region or client build.
    """
    if path is None:
        return {}, None
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict) or set(value) != {
        "schemaVersion", "appliesToBuild", "observedAt", "source", "servers"
    } or value.get("schemaVersion") != MARKET_DIMENSIONS_SCHEMA_VERSION:
        raise ValueError("market_dimensions_schema_mismatch")
    applies_to_build = value.get("appliesToBuild")
    if (
        not isinstance(build, int)
        or isinstance(build, bool)
        or build <= 0
        or not isinstance(applies_to_build, int)
        or isinstance(applies_to_build, bool)
        or applies_to_build <= 0
        or applies_to_build != build
    ):
        raise ValueError("market_dimensions_build_mismatch")
    try:
        _timestamp(value.get("observedAt"))
    except (AttributeError, TypeError, ValueError) as error:
        raise ValueError("market_dimensions_source_invalid") from error
    source = value.get("source")
    required_source_fields = {
        "kind", "authority", "reference", "evidenceSha256"
    }
    allowed_source_fields = required_source_fields | {
        "registryReference", "registrySha256", "fullScanSha256"
    }
    if (
        not isinstance(source, dict)
        or not required_source_fields.issubset(source)
        or set(source) - allowed_source_fields
    ):
        raise ValueError("market_dimensions_source_invalid")
    if source.get("kind") not in {"official-contract", "client-derived"}:
        raise ValueError("market_dimensions_source_invalid")
    if not isinstance(source.get("authority"), str) or not source["authority"].strip():
        raise ValueError("market_dimensions_source_invalid")
    if not isinstance(source.get("reference"), str) or not source["reference"].strip():
        raise ValueError("market_dimensions_source_invalid")
    if not re.fullmatch(r"[0-9a-f]{64}", str(source.get("evidenceSha256", ""))):
        raise ValueError("market_dimensions_source_invalid")
    for digest_field in ("registrySha256", "fullScanSha256"):
        if digest_field in source and not re.fullmatch(
            r"[0-9a-f]{64}", str(source[digest_field])
        ):
            raise ValueError("market_dimensions_source_invalid")
    if "registryReference" in source and (
        not isinstance(source["registryReference"], str)
        or not source["registryReference"].strip()
    ):
        raise ValueError("market_dimensions_source_invalid")
    rows = value.get("servers")
    if not isinstance(rows, list) or not rows or len(rows) > 1_000:
        raise ValueError("market_dimensions_rows_invalid")
    dimensions: dict[int, dict[str, Any]] = {}
    for row in rows:
        if not isinstance(row, dict) or not {
            "gameServerId", "region", "name", "faction", "market"
        }.issubset(row) or set(row) - {
            "gameServerId", "region", "name", "nameRu", "faction", "market",
            "dimensionsConfirmed",
        }:
            raise ValueError("market_dimensions_row_invalid")
        server_id = row.get("gameServerId")
        region, name, faction, market = (
            row.get("region"), row.get("name"), row.get("faction"), row.get("market")
        )
        dimensions_confirmed = row.get("dimensionsConfirmed", True)
        if (
            type(server_id) is not int
            or server_id <= 0
            or server_id in dimensions
            or not all(isinstance(label, str) and label.strip() and len(label) <= 80
                       for label in (region, name, faction))
            or market not in {"server", "cross-server", "unconfirmed"}
            or ("nameRu" in row and (
                not isinstance(row["nameRu"], str) or not row["nameRu"].strip()
                or len(row["nameRu"].strip()) > 80
            ))
            or ("dimensionsConfirmed" in row and type(row["dimensionsConfirmed"]) is not bool)
            or (dimensions_confirmed is True and market == "unconfirmed")
            or (dimensions_confirmed is False and market != "unconfirmed")
        ):
            raise ValueError("market_dimensions_row_invalid")
        dimensions[server_id] = {
            "region": region.strip(),
            "name": name.strip(),
            "nameRu": str(row.get("nameRu", name)).strip(),
            "faction": faction.strip(),
            "market": market,
            "dimensionsConfirmed": dimensions_confirmed,
        }
    source_summary = (
        f"{source['kind']}:{source['authority']}:{source['reference']}"
        f" observed {value['observedAt']} evidence sha256:{source['evidenceSha256']}"
    )
    return dimensions, source_summary


def _load_item_icons(path: Path | None, build: int | None) -> dict[int, str]:
    """Load only build-scoped, content-hashed client icon URLs."""
    if path is None:
        return {}
    value = json.loads(path.read_text(encoding="utf-8"))
    if (
        not isinstance(value, dict)
        or value.get("schemaVersion") != ITEM_ICONS_SCHEMA_VERSION
        or value.get("appliesToBuild") != build
        or not isinstance(value.get("items"), list)
    ):
        raise ValueError("item_icons_schema_mismatch")
    result: dict[int, str] = {}
    for row in value["items"]:
        if not isinstance(row, dict):
            raise ValueError("item_icons_row_invalid")
        item_id, url, digest = row.get("itemId"), row.get("url"), row.get("sha256")
        if (
            type(item_id) is not int
            or item_id <= 0
            or item_id in result
            or not isinstance(url, str)
            or not re.fullmatch(r"/assets/item-[0-9]+-[0-9a-f]{8,64}\.png", url)
            or not isinstance(digest, str)
            or not re.fullmatch(r"[0-9a-f]{64}", digest)
        ):
            raise ValueError("item_icons_row_invalid")
        result[item_id] = url
    return result


def _require_active_item_icons(
    icon_urls: dict[int, str], active_item_ids: set[int]
) -> None:
    """Fail before publication when an observed market row has no real icon."""
    missing = sorted(active_item_ids - set(icon_urls))
    if missing:
        sample = ",".join(str(item_id) for item_id in missing[:32])
        raise ValueError(
            f"active_item_icons_missing:{len(missing)}:{sample}"
        )


def _require_active_item_names(
    catalog_by_id: dict[int, Any], active_item_ids: set[int]
) -> None:
    missing: list[int] = []
    for item_id in sorted(active_item_ids):
        row = catalog_by_id.get(item_id)
        name = row["display_name"] if row is not None else None
        if (
            not isinstance(name, str)
            or not name.strip()
            or re.fullmatch(rf"Item\s+{item_id}", name.strip(), re.IGNORECASE)
        ):
            missing.append(item_id)
    if missing:
        sample = ",".join(str(item_id) for item_id in missing[:32])
        raise ValueError(
            f"active_item_names_missing:{len(missing)}:{sample}"
        )


def build_snapshot(
    database: Path, *, build: int | None = None, recipes_path: Path | None = None,
    market_rules_path: Path | None = None, market_dimensions_path: Path | None = None,
    item_icons_path: Path | None = None, game_server_id: int | None = None,
    restrict_to_market_dimensions: bool = False,
) -> dict[str, Any]:
    connection = sqlite3.connect(f"file:{database.resolve()}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        catalog = connection.execute(
            "SELECT applies_to_build,recovered_item_count FROM catalogs ORDER BY imported_at DESC LIMIT 1"
        ).fetchone()
        if build is None and catalog is not None:
            build = int(catalog["applies_to_build"])
        histories = _history(connection)
        detected_server_ids = sorted({
            int(row[0]) for row in connection.execute("SELECT DISTINCT game_server_id FROM scans")
        } | {
            int(row[0]) for row in connection.execute("SELECT DISTINCT game_server_id FROM summary_runs")
        })
        market_dimensions, dimension_source = _load_market_dimensions(
            market_dimensions_path, build
        )
        if game_server_id is not None:
            if game_server_id not in detected_server_ids:
                raise ValueError("economy_snapshot_server_not_observed")
            server_ids = [game_server_id]
        elif restrict_to_market_dimensions:
            if market_dimensions_path is None:
                raise ValueError("economy_snapshot_market_dimensions_required")
            missing_server_ids = sorted(set(market_dimensions) - set(detected_server_ids))
            if missing_server_ids:
                raise ValueError(
                    "economy_snapshot_market_dimension_not_observed:"
                    + ",".join(str(server_id) for server_id in missing_server_ids)
                )
            server_ids = sorted(market_dimensions)
        else:
            server_ids = detected_server_ids
        summary: dict[tuple[int, int], dict[str, Any]] = {}
        newly_liquid_at: dict[tuple[int, int], str] = {}
        detailed: dict[tuple[int, int], list[float]] = defaultdict(list)
        latest_observations: list[str] = []
        complete_summary_servers: set[int] = set()
        latest_summary_observed_at: dict[int, str] = {}
        for server_id in server_ids:
            summary_run = connection.execute(
                """SELECT summary_id,observed_at FROM summary_runs
                   WHERE game_server_id=?
                   ORDER BY observed_at DESC,summary_id DESC LIMIT 1""",
                (server_id,),
            ).fetchone()
            if summary_run is not None:
                complete_summary_servers.add(server_id)
                latest_summary_observed_at[server_id] = str(summary_run["observed_at"])
                latest_observations.append(str(summary_run["observed_at"]))
                previous_run = connection.execute(
                    """SELECT summary_id FROM summary_runs
                       WHERE game_server_id=? AND (observed_at < ? OR (observed_at=? AND summary_id < ?))
                       ORDER BY observed_at DESC,summary_id DESC LIMIT 1""",
                    (server_id, str(summary_run["observed_at"]), str(summary_run["observed_at"]),
                     int(summary_run["summary_id"])),
                ).fetchone()
                previous_active = set()
                if previous_run is not None:
                    previous_active = {
                        int(row[0]) for row in connection.execute(
                            "SELECT item_id FROM summary_prices WHERE summary_id=? AND sale_count>0",
                            (int(previous_run["summary_id"]),),
                        )
                    }
                for row in connection.execute(
                    """SELECT item_id,min(CAST(min_unit_price AS REAL)) AS minimum,
                              sum(sale_count) AS sale_count
                       FROM summary_prices WHERE summary_id=? GROUP BY item_id""",
                    (int(summary_run["summary_id"]),),
                ):
                    summary[(server_id, int(row["item_id"]))] = {
                        "minimum": _number(row["minimum"]),
                        "sale_count": int(row["sale_count"]),
                        "observed_at": str(summary_run["observed_at"]),
                    }
                    if previous_run is not None and int(row["sale_count"]) > 0 and int(row["item_id"]) not in previous_active:
                        newly_liquid_at[(server_id, int(row["item_id"]))] = str(summary_run["observed_at"])
            scan = connection.execute(
                """SELECT scan_id,observed_at FROM scans WHERE game_server_id=?
                   ORDER BY observed_at DESC,scan_id DESC LIMIT 1""",
                (server_id,),
            ).fetchone()
            if scan is not None:
                latest_observations.append(str(scan["observed_at"]))
                for row in connection.execute(
                    """WITH current_rows AS (
                         SELECT l.item_id,l.sale_id,l.unit_price,l.scan_id,
                                row_number() OVER (
                                  PARTITION BY l.item_id,l.sale_id ORDER BY l.scan_id DESC
                                ) AS position
                         FROM listings l JOIN scans s USING(scan_id)
                         JOIN item_server_state st
                           ON st.game_server_id=s.game_server_id
                          AND st.item_id=l.item_id
                          AND st.current_generation=l.generation
                         WHERE s.game_server_id=?
                       )
                       SELECT item_id,CAST(unit_price AS REAL) AS unit_price
                       FROM current_rows WHERE position=1""",
                    (server_id,),
                ):
                    detailed[(server_id, int(row["item_id"]))].append(float(row["unit_price"]))

        item_columns = {str(row[1]) for row in connection.execute("PRAGMA table_info(items)")}
        state_columns = {
            str(row[1]) for row in connection.execute("PRAGMA table_info(item_server_state)")
        }
        current_states: dict[tuple[int, int], dict[str, Any]] = {}
        if {"game_server_id", "item_id", "scan_status", "last_scanned_at",
            "last_listing_count"}.issubset(state_columns):
            placeholders = ",".join("?" for _ in server_ids)
            for state in connection.execute(
                f"""SELECT game_server_id,item_id,scan_status,last_scanned_at,last_listing_count
                    FROM item_server_state WHERE game_server_id IN ({placeholders})""",
                server_ids,
            ):
                current_states[(int(state[0]), int(state[1]))] = {
                    "status": str(state[2]),
                    "observed_at": str(state[3]) if state[3] else None,
                    "listing_count": int(state[4]),
                }
        optional = lambda name: name if name in item_columns else f"NULL AS {name}"
        item_rows = connection.execute(
            f"""SELECT item_id,display_name,{optional('display_name_ru')},internal_name,
                       item_type,item_level,{optional('icon_name')},{optional('grade')},
                       {optional('tier')},last_listing_count FROM items ORDER BY item_id"""
        ).fetchall()
        catalog_by_id = {int(row["item_id"]): row for row in item_rows}
        observed_catalog_ids = {
            item_id
            for _, item_id in set(summary) | set(detailed) | set(histories)
        }
        _require_active_item_names(catalog_by_id, observed_catalog_ids)
        icon_urls = _load_item_icons(item_icons_path, build)
        if item_icons_path is not None:
            _require_active_item_icons(icon_urls, observed_catalog_ids)
        catalog_items = [{
            "itemId": int(row["item_id"]),
            "name": str(row["display_name"]),
            "names": {
                "en": str(row["display_name"]),
                "ru": str(row["display_name_ru"] or row["display_name"]),
            },
            "internalName": str(row["internal_name"]),
            "category": str(row["item_type"]),
            "itemLevel": int(row["item_level"]),
            "iconName": str(row["icon_name"] or ""),
            "iconUrl": icon_urls.get(int(row["item_id"])),
            "grade": str(row["grade"] or ""),
            "tier": str(row["tier"] or ""),
        } for row in item_rows]
        items: list[dict[str, Any]] = []
        for server_id in server_ids:
            dimension = market_dimensions.get(server_id)
            observed_item_ids = {
                item_id for candidate_server, item_id in summary if candidate_server == server_id
            } | {
                item_id for candidate_server, item_id in detailed if candidate_server == server_id
            } | {
                item_id for candidate_server, item_id in histories if candidate_server == server_id
            }
            max_sales = max(
                (entry["sale_count"] for (candidate_server, _), entry in summary.items()
                 if candidate_server == server_id),
                default=0,
            )
            for item_id in sorted(observed_item_ids):
                row = catalog_by_id.get(item_id)
                if row is None:
                    continue
                current_summary = summary.get((server_id, item_id))
                detail_prices = detailed.get((server_id, item_id), [])
                points = histories.get((server_id, item_id), [])
                current_state = current_states.get((server_id, item_id))
                summary_boundary = (
                    current_summary["observed_at"]
                    if current_summary is not None
                    else latest_summary_observed_at.get(server_id)
                )
                detail_is_current = bool(
                    current_state
                    and current_state["status"] in {"scanned_empty", "scanned_listed"}
                    and current_state["observed_at"]
                    and (
                        summary_boundary is None
                        or _timestamp(current_state["observed_at"])
                        >= _timestamp(summary_boundary)
                    )
                )
                if detail_is_current:
                    minimum = _number(min(detail_prices)) if detail_prices else None
                    sale_count = len(detail_prices)
                elif current_summary is not None:
                    minimum = current_summary["minimum"]
                    sale_count = current_summary["sale_count"]
                elif server_id in complete_summary_servers:
                    minimum = None
                    sale_count = 0
                else:
                    minimum = (
                        _number(min(detail_prices)) if detail_prices else (
                            points[-1].value if points else None
                        )
                    )
                    sale_count = len(detail_prices)
                detail_median = (
                    _number(median(detail_prices))
                    if detail_is_current and detail_prices
                    else None
                )
                listing_count = len(detail_prices) if detail_is_current else sale_count
                liquidity = _liquidity_score(sale_count, max_sales)
                item_observations = [point.at for point in points]
                if detail_is_current:
                    item_observations.append(_timestamp(current_state["observed_at"]))
                elif current_summary:
                    item_observations.append(_timestamp(current_summary["observed_at"]))
                last_observed_at = (
                    max(item_observations).isoformat().replace("+00:00", "Z")
                    if item_observations else None
                )
                items.append({
                    "itemId": item_id,
                    "name": str(row["display_name"]),
                    "names": {
                        "en": str(row["display_name"]),
                        "ru": str(row["display_name_ru"] or row["display_name"]),
                    },
                    "internalName": str(row["internal_name"]),
                    "category": str(row["item_type"]),
                    "itemLevel": int(row["item_level"]),
                    "iconName": str(row["icon_name"] or ""),
                    "iconUrl": icon_urls.get(item_id),
                    "grade": str(row["grade"] or ""),
                    "tier": str(row["tier"] or ""),
                    "serverId": server_id,
                    "market": dimension["market"] if dimension else "unconfirmed",
                    "lastObservedAt": last_observed_at,
                    "current": minimum,
                    "minimum": minimum,
                    "median": detail_median,
                    "saleCount": sale_count,
                    "listingCount": listing_count,
                    "newlyLiquid": (server_id, item_id) in newly_liquid_at,
                    "newlyLiquidAt": newly_liquid_at.get((server_id, item_id)),
                    "liquidityScore": round(liquidity, 4),
                    "change1h": _change(points, timedelta(hours=1)),
                    "change24h": _change(points, timedelta(hours=24)),
                    "change7d": _change(points, timedelta(days=7)),
                    "history": [point.json() for point in points],
                })

        servers: list[dict[str, Any]] = []
        for server_id in server_ids:
            server_items = [item for item in items if item["serverId"] == server_id and item["saleCount"]]
            active_observations = sorted(
                item["lastObservedAt"] for item in server_items if item["lastObservedAt"]
            )
            observed = connection.execute(
                """SELECT max(observed_at) FROM (
                     SELECT observed_at FROM scans WHERE game_server_id=?
                     UNION ALL SELECT observed_at FROM summary_runs WHERE game_server_id=?)""",
                (server_id, server_id),
            ).fetchone()[0]
            if server_id in complete_summary_servers:
                coverage_items = len(catalog_items)
            else:
                try:
                    coverage_items = int(connection.execute(
                        """SELECT count(*) FROM item_server_state
                           WHERE game_server_id=? AND
                             scan_status IN ('scanned_empty','scanned_listed')""",
                        (server_id,),
                    ).fetchone()[0])
                except sqlite3.Error:
                    coverage_items = 0
            dimension = market_dimensions.get(server_id)
            fallback_names = {
                "en": f"Observed market · {server_id}",
                "ru": f"Наблюдаемый рынок · {server_id}",
            }
            dimension_name = dimension["name"] if dimension else fallback_names["en"]
            servers.append({
                "region": dimension["region"] if dimension else "unconfirmed",
                "serverId": server_id,
                "name": dimension_name,
                "names": {
                    "en": dimension_name,
                    "ru": dimension["nameRu"] if dimension else fallback_names["ru"],
                },
                "faction": dimension["faction"] if dimension else "unknown",
                "market": dimension["market"] if dimension else "unconfirmed",
                "dimensionsConfirmed": bool(dimension and dimension["dimensionsConfirmed"]),
                "dimensionSource": dimension_source if dimension else None,
                "lastObservedAt": observed,
                "oldestActiveObservedAt": active_observations[0] if active_observations else None,
                "newestActiveObservedAt": active_observations[-1] if active_observations else None,
                "coverageItems": coverage_items,
                "activeItems": len(server_items),
                "listings": sum(item["saleCount"] for item in server_items),
            })

        recipes = _load_recipes(
            recipes_path,
            build,
            {
                int(row["item_id"]): {
                    "en": str(row["display_name"]),
                    "ru": str(row["display_name_ru"] or row["display_name"]),
                }
                for row in item_rows
            },
        )
        market_rules = _load_market_rules(market_rules_path, build)
        active_items = sum(1 for item in items if item["saleCount"] > 0)
        limitations = ["1h/24h/7d changes appear only after enough adjacent observations exist."]
        if any(not server["dimensionsConfirmed"] for server in servers):
            limitations.insert(
                0,
                "One or more server/region/faction/market labels have no build-scoped evidence and remain unconfirmed.",
            )
        if not recipes:
            limitations.append("Craft recipes were not supplied to this snapshot export.")
        generated_at = max(latest_observations, default=datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"))
        snapshot = {
            "schemaVersion": SCHEMA_VERSION,
            "generatedAt": generated_at,
            "build": build,
            "stats": {
                "catalogItems": (
                    len(catalog_items)
                ),
                "activeItems": active_items,
                "listings": sum(item["saleCount"] for item in items),
                "servers": len(servers),
            },
            "items": items,
            "catalog": catalog_items,
            "recipes": recipes,
            "servers": servers,
            "farmSessions": [],
            "marketRules": market_rules,
            "limitations": limitations,
        }
        canonical = json.dumps(
            snapshot, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        snapshot["revision"] = _revision(build, generated_at, sha256(canonical).hexdigest())
        return snapshot
    finally:
        connection.close()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--build", type=int)
    parser.add_argument("--recipes", type=Path)
    parser.add_argument("--market-rules", type=Path)
    parser.add_argument("--market-dimensions", type=Path)
    parser.add_argument("--item-icons", type=Path)
    parser.add_argument("--game-server-id", type=int)
    parser.add_argument("--restrict-to-market-dimensions", action="store_true")
    args = parser.parse_args()
    snapshot = build_snapshot(
        args.database, build=args.build, recipes_path=args.recipes,
        market_rules_path=args.market_rules, market_dimensions_path=args.market_dimensions,
        item_icons_path=args.item_icons, game_server_id=args.game_server_id,
        restrict_to_market_dimensions=args.restrict_to_market_dimensions,
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(snapshot, ensure_ascii=False, separators=(",", ":")),
        encoding="utf-8",
    )
    print(json.dumps({
        "status": "economy_snapshot_ready",
        "schema": SCHEMA_VERSION,
        "items": snapshot["stats"]["catalogItems"],
        "active_items": snapshot["stats"]["activeItems"],
        "listings": snapshot["stats"]["listings"],
        "servers": snapshot["stats"]["servers"],
        "recipes": len(snapshot["recipes"]),
        "output": str(args.output),
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
