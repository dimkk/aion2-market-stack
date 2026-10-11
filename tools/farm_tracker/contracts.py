"""Strict semantic input and aggregate export contracts."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import re
from typing import Any


EVENT_SCHEMA = "a2.decoded.economy-event.v1"
EXPORT_SCHEMA = "a2.farm.session-export.v1"
SAFE_ID = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")
SAFE_SOURCE = re.compile(r"^[A-Za-z0-9._:+/-]{1,96}$")
MAX_TIME_MS = 4_102_444_800_000


class ContractError(ValueError):
    """A bounded machine-readable contract error."""


@dataclass(frozen=True)
class EventSource:
    module_id: str
    module_version: str
    bus_session_id: str


@dataclass(frozen=True)
class DecodedEconomyEvent:
    schema: str
    event_id: str
    observed_at_ms: int
    source: EventSource
    kind: str
    delta: int | None = None
    item_id: int | None = None
    quantity: int | None = None
    map_id: int | None = None
    zone_id: int | None = None
    label: str | None = None

    @classmethod
    def parse(cls, value: Any) -> "DecodedEconomyEvent":
        if not isinstance(value, dict) or value.get("schema") != EVENT_SCHEMA:
            raise ContractError("economy_event_schema_invalid")
        event_id = value.get("event_id")
        observed_at_ms = value.get("observed_at_ms")
        raw_source = value.get("source")
        kind = value.get("kind")
        if not isinstance(event_id, str) or SAFE_ID.fullmatch(event_id) is None:
            raise ContractError("economy_event_id_invalid")
        if (
            not isinstance(observed_at_ms, int)
            or isinstance(observed_at_ms, bool)
            or not 0 < observed_at_ms <= MAX_TIME_MS
        ):
            raise ContractError("economy_event_time_invalid")
        if not isinstance(raw_source, dict) or set(raw_source) != {
            "module_id", "module_version", "bus_session_id"
        }:
            raise ContractError("economy_event_source_invalid")
        source_values = [
            raw_source.get("module_id"), raw_source.get("module_version"),
            raw_source.get("bus_session_id"),
        ]
        if any(not isinstance(item, str) or SAFE_SOURCE.fullmatch(item) is None for item in source_values):
            raise ContractError("economy_event_source_invalid")
        source = EventSource(*source_values)
        common = {"schema", "event_id", "observed_at_ms", "source", "kind"}
        if kind == "heartbeat":
            if set(value) != common:
                raise ContractError("economy_event_shape_invalid")
            return cls(EVENT_SCHEMA, event_id, observed_at_ms, source, kind)
        if kind == "kina_delta":
            if set(value) != common | {"currency", "delta"} or value.get("currency") != "kina":
                raise ContractError("economy_event_shape_invalid")
            delta = value.get("delta")
            if not isinstance(delta, int) or isinstance(delta, bool) or abs(delta) > 10**15:
                raise ContractError("economy_event_delta_invalid")
            return cls(EVENT_SCHEMA, event_id, observed_at_ms, source, kind, delta=delta)
        if kind == "loot":
            if set(value) != common | {"owner_scope", "item_id", "quantity"} or value.get("owner_scope") != "local_player":
                raise ContractError("economy_event_shape_invalid")
            item_id, quantity = value.get("item_id"), value.get("quantity")
            if (
                not isinstance(item_id, int) or isinstance(item_id, bool) or not 0 < item_id <= 0xFFFFFFFF
                or not isinstance(quantity, int) or isinstance(quantity, bool) or not 0 < quantity <= 10**9
            ):
                raise ContractError("economy_event_loot_invalid")
            return cls(EVENT_SCHEMA, event_id, observed_at_ms, source, kind, item_id=item_id, quantity=quantity)
        if kind in {"zone_identity", "zone_map_identity"}:
            expected = common | ({"map_id", "zone_id", "label"} if kind == "zone_identity" else {"map_id", "label"})
            if set(value) != expected:
                raise ContractError("economy_event_shape_invalid")
            map_id, zone_id, label = value.get("map_id"), value.get("zone_id"), value.get("label")
            if not isinstance(map_id, int) or isinstance(map_id, bool) or not 0 < map_id <= 0xFFFFFFFF:
                raise ContractError("economy_event_zone_invalid")
            if kind == "zone_identity" and (
                not isinstance(zone_id, int) or isinstance(zone_id, bool) or not 0 < zone_id <= 0xFFFFFFFF
            ):
                raise ContractError("economy_event_zone_invalid")
            if not isinstance(label, str) or not label.strip() or len(label.encode("utf-8")) > 80:
                raise ContractError("economy_event_zone_invalid")
            return cls(EVENT_SCHEMA, event_id, observed_at_ms, source, kind, map_id=map_id, zone_id=zone_id, label=label.strip())
        raise ContractError("economy_event_kind_invalid")

    def as_dict(self) -> dict[str, Any]:
        root: dict[str, Any] = {
            "schema": self.schema,
            "event_id": self.event_id,
            "observed_at_ms": self.observed_at_ms,
            "source": {
                "module_id": self.source.module_id,
                "module_version": self.source.module_version,
                "bus_session_id": self.source.bus_session_id,
            },
            "kind": self.kind,
        }
        if self.kind == "kina_delta":
            root.update(currency="kina", delta=self.delta)
        elif self.kind == "loot":
            root.update(owner_scope="local_player", item_id=self.item_id, quantity=self.quantity)
        elif self.kind == "zone_identity":
            root.update(map_id=self.map_id, zone_id=self.zone_id, label=self.label)
        elif self.kind == "zone_map_identity":
            root.update(map_id=self.map_id, label=self.label)
        return root


def build_export(session: dict[str, Any]) -> dict[str, Any]:
    """Build the exact deterministic contract accepted by Economy API."""
    activity = session.get("activity")
    ordered_session = {
        "session_id": session["session_id"],
        "activity": activity,
        "started_at_ms": session["started_at_ms"],
        "stopped_at_ms": session["stopped_at_ms"],
        "observed_direct_kina": session["observed_direct_kina"],
        "estimated_loot_value": session["estimated_loot_value"],
        "estimated_total_value": session["estimated_total_value"],
        "event_count": session["event_count"],
        "loot": [
            {
                "item_id": item["item_id"],
                "quantity": item["quantity"],
                "unit_price": item["unit_price"],
                "market_server_id": item["market_server_id"],
                "price_observed_at_ms": item["price_observed_at_ms"],
                "price_source": item["price_source"],
                "estimated_value": item["estimated_value"],
            }
            for item in session["loot"]
        ],
    }
    unsigned = {"schema": EXPORT_SCHEMA, "session": ordered_session}
    canonical = json.dumps(unsigned, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return {"schema": EXPORT_SCHEMA, "payload_id": hashlib.sha256(canonical).hexdigest(), "session": ordered_session}
