#!/usr/bin/env python3
"""Split one sanitized Economy snapshot into protected deployable views."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any


SCHEMA_VERSION = "aion2.economy.snapshot/v1"
# The publisher, API ingestion path and protected readback contract all accept
# one immutable economy view up to 8 MiB.  Keep the builder on that same bound;
# the former 900 KiB HTTP-upload workaround is obsolete now that production
# publishes immutable views directly through the project-scoped YC route.
MAX_VIEW_BYTES = 8 * 1024 * 1024
MAX_SEGMENTS = 64


def _market_segment(server_id: Any) -> str:
    if type(server_id) is not int or server_id <= 0:
        raise ValueError("snapshot_item_server_invalid")
    return f"s{server_id // 100}"


def _encoded_size(value: Any) -> int:
    return len(
        json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    )


def _split_segment_items(
    base_segment: str,
    rows: list[dict[str, Any]],
    common: dict[str, Any],
) -> list[tuple[str, dict[str, Any]]]:
    """Return deterministic, size-bounded view parts for one server group."""
    empty_view = {**common, "items": []}
    empty_size = _encoded_size(empty_view)
    chunks: list[list[dict[str, Any]]] = []
    current: list[dict[str, Any]] = []
    current_size = empty_size

    for row in rows:
        row_size = _encoded_size(row)
        candidate_size = current_size + row_size + (1 if current else 0)
        if candidate_size > MAX_VIEW_BYTES:
            if not current:
                raise ValueError(f"view_item_too_large:{base_segment}")
            chunks.append(current)
            current = []
            current_size = empty_size
            candidate_size = current_size + row_size
            if candidate_size > MAX_VIEW_BYTES:
                raise ValueError(f"view_item_too_large:{base_segment}")
        current.append(row)
        current_size = candidate_size
    if current:
        chunks.append(current)

    if len(chunks) == 1:
        return [(base_segment, {**common, "items": chunks[0]})]
    return [
        (f"{base_segment}_{index:02d}", {**common, "items": chunk})
        for index, chunk in enumerate(chunks, start=1)
    ]


def _segmented_views(
    rows: list[dict[str, Any]], common: dict[str, Any]
) -> list[tuple[str, dict[str, Any]]]:
    base_segments = sorted({_market_segment(row.get("serverId")) for row in rows})
    result: list[tuple[str, dict[str, Any]]] = []
    for base_segment in base_segments:
        segment_rows = [
            row for row in rows if _market_segment(row.get("serverId")) == base_segment
        ]
        result.extend(_split_segment_items(base_segment, segment_rows, common))
    if not 1 <= len(result) <= MAX_SEGMENTS:
        raise ValueError("view_segments_invalid")
    return result


def build_views(snapshot: dict[str, Any]) -> dict[str, dict[str, Any]]:
    if snapshot.get("schemaVersion") != SCHEMA_VERSION:
        raise ValueError("snapshot_schema_mismatch")
    revision = snapshot.get("revision")
    if not isinstance(revision, str) or not revision or len(revision) > 128:
        raise ValueError("snapshot_revision_missing")
    for key in ("items", "recipes", "servers", "farmSessions", "limitations"):
        if not isinstance(snapshot.get(key), list):
            raise ValueError(f"snapshot_{key}_invalid")

    common = {
        "revision": revision,
        "schemaVersion": snapshot["schemaVersion"],
        "generatedAt": snapshot.get("generatedAt"),
        "build": snapshot.get("build"),
    }
    items = snapshot["items"]
    active = [row for row in items if row.get("current") is not None and row.get("saleCount", 0) > 0]
    stats = snapshot.get("stats")
    if (
        not isinstance(stats, dict)
        or type(stats.get("activeItems")) is not int
        or stats["activeItems"] != len(active)
    ):
        raise ValueError("snapshot_active_item_count_mismatch")
    comparable_24h = [
        row for row in active
        if isinstance(row.get("change24h"), (int, float)) and not isinstance(row.get("change24h"), bool)
    ]
    movers = {
        "up": sorted(comparable_24h, key=lambda row: row["change24h"], reverse=True)[:100],
        "down": sorted(comparable_24h, key=lambda row: row["change24h"])[:100],
        "new": sorted(
            (row for row in active if row.get("newlyLiquid") is True),
            key=lambda row: row.get("newlyLiquidAt") or "", reverse=True,
        )[:100],
        "liquid": sorted(active, key=lambda row: row.get("liquidityScore", 0), reverse=True)[:100],
        "listings": sorted(active, key=lambda row: row.get("listingCount", 0), reverse=True)[:100],
    }
    views = {
        # The protected market route is an active-listings view. Historical
        # rows may remain in the canonical snapshot/items view so item history
        # is not lost, but they must not inflate current market coverage.
        "market.default.json": {**snapshot, "items": active},
        "items.default.json": {**common, "items": items, "itemCount": len(items)},
        "movers.default.json": {**common, **movers},
        "servers.default.json": {**common, "servers": snapshot["servers"]},
        "craft.default.json": {
            **common,
            "recipes": snapshot["recipes"],
            "marketRules": snapshot.get("marketRules", {
                "saleTaxRate": None, "saleTaxSource": None, "serverRates": []
            }),
        },
        "farm-value.default.json": {
            **common,
            "farmSessions": snapshot["farmSessions"],
            "marketRules": snapshot.get("marketRules", {
                "saleTaxRate": None, "saleTaxSource": None, "serverRates": []
            }),
            "limitations": [
                row for row in snapshot["limitations"]
                if isinstance(row, str) and "farm" in row.lower()
            ],
        },
    }
    if all(
        0 < _encoded_size(value) <= MAX_VIEW_BYTES
        for value in views.values()
    ):
        return views

    market_parts = _segmented_views(active, common)
    item_parts = _segmented_views(items, common)
    market_segments = [segment for segment, _ in market_parts]
    item_segments = [segment for segment, _ in item_parts]
    views["market.default.json"] = {
        **snapshot, "items": [], "segments": market_segments
    }
    views["items.default.json"] = {
        **common, "items": [], "segments": item_segments, "itemCount": len(items)
    }
    for segment, value in market_parts:
        views[f"market.{segment}.json"] = value
    for segment, value in item_parts:
        views[f"items.{segment}.json"] = value
    for name, value in views.items():
        encoded_size = _encoded_size(value)
        if not encoded_size or encoded_size > MAX_VIEW_BYTES:
            raise ValueError(f"view_too_large:{name}")
    return views


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--snapshot", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    snapshot = json.loads(args.snapshot.read_text(encoding="utf-8"))
    if not isinstance(snapshot, dict):
        raise SystemExit("snapshot_must_be_object")
    views = build_views(snapshot)
    args.output.mkdir(parents=True, exist_ok=True)
    for name, value in views.items():
        encoded = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
        (args.output / name).write_text(encoded, encoding="utf-8")
        print(f"economy_view_ready name={name} bytes={len(encoded.encode('utf-8'))}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
