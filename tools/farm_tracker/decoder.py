"""Strict decoder for the confirmed farm-related server packet shapes."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import math
from pathlib import Path
import struct
from typing import Any

from .contracts import EVENT_SCHEMA
from .quotes import ItemCatalog


ITEM_INCREMENT_OPCODE = 0x8D33
INVENTORY_UPDATE_OPCODE = 0x561B
KINA_ITEM_ID = 930_100_031
ZONE_TRANSITION_OPCODE = 0x3623
MAP_CONTEXT_OPCODES = {0x4001: 17, 0x4002: 8}
ZONE_CONTEXT_MAX_DELAY_MS = 5_000


@dataclass(frozen=True)
class PacketIdentity:
    observed_at_ms: int
    flow_id: int
    tcp_seq: int
    frame_index: int


class ZoneCatalog:
    def __init__(self, path: Path | None, expected_build: int | None = None) -> None:
        self.maps: dict[int, dict[str, Any]] = {}
        if path is None:
            return
        value = json.loads(path.read_text(encoding="utf-8"))
        if value.get("schema") != "a2.farm.zone-catalog.v1" or not isinstance(value.get("maps"), list):
            raise ValueError("zone_catalog_invalid")
        if expected_build is not None and value.get("source_build") != expected_build:
            raise ValueError("zone_catalog_build_mismatch")
        for row in value["maps"]:
            if isinstance(row, dict) and isinstance(row.get("map_id"), int):
                self.maps[row["map_id"]] = row

    @staticmethod
    def _inside(points: list[list[float]], x: float, y: float) -> bool:
        inside = False
        j = len(points) - 1
        for i, point in enumerate(points):
            xi, yi = float(point[0]), float(point[1])
            xj, yj = float(points[j][0]), float(points[j][1])
            if ((yi > y) != (yj > y)) and x < (xj - xi) * (y - yi) / (yj - yi) + xi:
                inside = not inside
            j = i
        return inside

    def resolve(self, map_id: int, coordinates: tuple[float, float, float]) -> dict[str, Any] | None:
        row = self.maps.get(map_id)
        if row is None:
            return None
        x, y, z = coordinates
        candidates: list[dict[str, Any]] = []
        for volume in row.get("volumes", []):
            if not isinstance(volume, dict):
                continue
            bounds, points = volume.get("bounds"), volume.get("points")
            if not isinstance(bounds, list) or len(bounds) != 4 or not isinstance(points, list) or len(points) < 3:
                continue
            if not float(volume.get("height_min", math.inf)) <= z <= float(volume.get("height_max", -math.inf)):
                continue
            if not float(bounds[0]) <= x <= float(bounds[2]) or not float(bounds[1]) <= y <= float(bounds[3]):
                continue
            if self._inside(points, x, y):
                candidates.append(volume)
        if len(candidates) == 1:
            return candidates[0]
        if len(candidates) > 1 and all(isinstance(item.get("priority"), int) and item.get("priority_source") for item in candidates):
            ranked = sorted(candidates, key=lambda item: int(item["priority"]), reverse=True)
            if int(ranked[0]["priority"]) != int(ranked[1]["priority"]):
                return ranked[0]
        return None

    def map_label(self, map_id: int) -> str | None:
        row = self.maps.get(map_id)
        label = row.get("label") if row else None
        return label if isinstance(label, str) and label.strip() else None


class EconomyPacketDecoder:
    def __init__(self, bus_session_id: str, catalog: ItemCatalog, zones: ZoneCatalog | None = None) -> None:
        self.bus_session_id = bus_session_id
        self.catalog = catalog
        self.item_ids = set(catalog.names)
        self.zones = zones or ZoneCatalog(None)
        self.balances: dict[tuple[int, int], int] = {}
        self.pending_zones: dict[int, tuple[PacketIdentity, tuple[float, float, float]]] = {}

    def forget_flow(self, flow_id: int) -> None:
        self.balances = {key: value for key, value in self.balances.items() if key[0] != flow_id}
        self.pending_zones.pop(flow_id, None)

    def _id(self, identity: PacketIdentity, *parts: bytes) -> str:
        digest = hashlib.sha256()
        digest.update(self.bus_session_id.encode("ascii", "strict"))
        digest.update(struct.pack("<QII", identity.flow_id, identity.tcp_seq, identity.frame_index))
        for part in parts:
            digest.update(part)
        return "farm:" + digest.hexdigest()

    def _source(self) -> dict[str, str]:
        return {
            "module_id": "a2.farm.packet-decoder",
            "module_version": "1.0.0",
            "bus_session_id": self.bus_session_id,
        }

    def heartbeat(self, identity: PacketIdentity) -> dict[str, Any]:
        return {
            "schema": EVENT_SCHEMA,
            "event_id": self._id(identity, struct.pack("<q", identity.observed_at_ms), b"heartbeat"),
            "observed_at_ms": identity.observed_at_ms,
            "source": self._source(),
            "kind": "heartbeat",
        }

    def decode(self, frame: bytes, identity: PacketIdentity) -> dict[str, Any] | None:
        if identity.observed_at_ms <= 0 or len(frame) < 2:
            return None
        opcode = struct.unpack_from("<H", frame)[0]
        if opcode == ZONE_TRANSITION_OPCODE:
            if len(frame) != 26 or frame[2] != 0:
                return None
            coordinates = struct.unpack_from("<fff", frame, 8)
            if not all(math.isfinite(value) for value in coordinates):
                return None
            self.pending_zones[identity.flow_id] = (identity, coordinates)
            return None
        if opcode in MAP_CONTEXT_OPCODES and len(frame) == MAP_CONTEXT_OPCODES[opcode]:
            pending = self.pending_zones.pop(identity.flow_id, None)
            if pending is None or not 0 <= identity.observed_at_ms - pending[0].observed_at_ms <= ZONE_CONTEXT_MAX_DELAY_MS:
                return None
            map_id = struct.unpack_from("<I", frame, 2)[0]
            zone = self.zones.resolve(map_id, pending[1])
            label = zone.get("label") if zone else self.zones.map_label(map_id)
            kind = "zone_identity" if zone else "zone_map_identity"
            if not label:
                return None
            event = {
                "schema": EVENT_SCHEMA,
                "event_id": self._id(identity, struct.pack("<HI", opcode, map_id), str(label).encode("utf-8")),
                "observed_at_ms": identity.observed_at_ms,
                "source": self._source(),
                "kind": kind,
                "map_id": map_id,
                "label": str(label),
            }
            if zone:
                event["zone_id"] = int(zone["zone_id"])
            return event
        if opcode == ITEM_INCREMENT_OPCODE:
            if len(frame) != 27:
                return None
            item_id, quantity = struct.unpack_from("<II", frame, 7)
            if item_id not in self.item_ids or not 0 < quantity <= 10**9:
                return None
            return {
                "schema": EVENT_SCHEMA,
                "event_id": self._id(identity, struct.pack("<HII", opcode, item_id, quantity)),
                "observed_at_ms": identity.observed_at_ms,
                "source": self._source(),
                "kind": "loot",
                "owner_scope": "local_player",
                "item_id": item_id,
                "quantity": quantity,
            }
        if opcode != INVENTORY_UPDATE_OPCODE or len(frame) not in {43, 127}:
            return None
        if len(frame) == 43:
            item_id, offset = struct.unpack_from("<I", frame, 13)[0], 13
        else:
            needle = struct.pack("<I", KINA_ITEM_ID)
            offset = frame.find(needle, 2)
            if offset < 0 or offset + 8 > len(frame):
                return None
            item_id = KINA_ITEM_ID
        if item_id != KINA_ITEM_ID and item_id not in self.item_ids:
            return None
        balance = struct.unpack_from("<I", frame, offset + 4)[0]
        key = (identity.flow_id, item_id)
        previous = self.balances.get(key)
        self.balances[key] = balance
        if previous is None or previous == balance:
            return None
        delta = balance - previous
        if item_id != KINA_ITEM_ID:
            if delta <= 0:
                return None
            return {
                "schema": EVENT_SCHEMA,
                "event_id": self._id(identity, struct.pack("<HIII", opcode, item_id, previous, balance)),
                "observed_at_ms": identity.observed_at_ms,
                "source": self._source(),
                "kind": "loot",
                "owner_scope": "local_player",
                "item_id": item_id,
                "quantity": delta,
            }
        return {
            "schema": EVENT_SCHEMA,
            "event_id": self._id(identity, struct.pack("<HIII", opcode, item_id, previous, balance)),
            "observed_at_ms": identity.observed_at_ms,
            "source": self._source(),
            "kind": "kina_delta",
            "currency": "kina",
            "delta": delta,
        }
