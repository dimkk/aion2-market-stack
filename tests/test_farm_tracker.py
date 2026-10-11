from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import socket
import sqlite3
import struct
import threading
import time

import pytest

from tools.farm_tracker.contracts import ContractError, DecodedEconomyEvent, EVENT_SCHEMA
from tools.farm_tracker.decoder import EconomyPacketDecoder, PacketIdentity, ZoneCatalog
from tools.farm_tracker.history import HistoryStore
from tools.farm_tracker.packet_bus import (
    BusRecord,
    CaptureBusSink,
    PacketBusWriter,
    EconomyStreamDecoder,
    read_header,
    read_record,
)
import tools.farm_tracker.packet_bus as packet_bus_module
from tools.farm_tracker.quotes import ItemCatalog, MarketPriceProvider
from tools.farm_tracker.session import FarmSessionTracker
from tools.farm_tracker.sync import sync_session
from tools.farm_tracker.app import Runtime


NOW = 1_790_000_000_000


def catalog_file(path: Path) -> Path:
    path.write_text(
        json.dumps({"items": [{"itemId": 123, "displayName": "Known item"}]}),
        encoding="utf-8",
    )
    return path


def event(event_id: str, kind: str, at: int, **values):
    root = {
        "schema": EVENT_SCHEMA,
        "event_id": event_id,
        "observed_at_ms": at,
        "source": {
            "module_id": "test.decoder",
            "module_version": "1.0.0",
            "bus_session_id": "test-bus",
        },
        "kind": kind,
    }
    root.update(values)
    return root


def market_database(path: Path) -> Path:
    with sqlite3.connect(path) as connection:
        connection.executescript(
            """
            CREATE TABLE summary_runs(summary_id TEXT PRIMARY KEY,observed_at TEXT,game_server_id INTEGER);
            CREATE TABLE summary_prices(summary_id TEXT,item_id INTEGER,min_unit_price INTEGER,sale_count INTEGER);
            CREATE TABLE scans(scan_id TEXT PRIMARY KEY,observed_at TEXT,game_server_id INTEGER);
            CREATE TABLE listings(scan_id TEXT,item_id INTEGER,generation INTEGER,unit_price INTEGER);
            CREATE TABLE item_server_state(game_server_id INTEGER,item_id INTEGER,current_generation INTEGER);
            INSERT INTO summary_runs VALUES('s','2026-09-21T14:13:20Z',12301);
            INSERT INTO summary_prices VALUES('s',123,777,2);
            """
        )
    return path


def tracker(tmp_path: Path) -> tuple[FarmSessionTracker, HistoryStore]:
    catalog = ItemCatalog(catalog_file(tmp_path / "items.json"))
    history = HistoryStore(tmp_path / "farm.sqlite", NOW - 1)
    prices = MarketPriceProvider(market_database(tmp_path / "market.sqlite"), 12301, 86_400)
    return FarmSessionTracker(history, prices, catalog, idle_seconds=15), history


def test_contract_rejects_extra_or_guessed_fields():
    value = event("event-1", "loot", NOW, owner_scope="local_player", item_id=123, quantity=1)
    parsed = DecodedEconomyEvent.parse(value)
    assert parsed.item_id == 123
    value["item_name"] = "untrusted"
    with pytest.raises(ContractError, match="economy_event_shape_invalid"):
        DecodedEconomyEvent.parse(value)


def test_session_auto_start_stop_quote_and_durable_deduplication(tmp_path: Path):
    subject, history = tracker(tmp_path)
    loot = event("loot-1", "loot", NOW, owner_scope="local_player", item_id=123, quantity=2)
    result = subject.apply(loot)
    assert result == {"applied": True, "auto_started": True, "auto_stopped": False}
    active = subject.snapshot()
    assert active["loot"][0]["unit_price"] == 777
    assert active["estimated_total_value"] == 1554

    assert subject.apply(loot)["applied"] is False
    assert subject.snapshot()["event_count"] == 1
    stopped = subject.expire(NOW + 15_000)
    assert stopped["status"] == "stopped"
    assert history.list()[0]["estimated_total_value"] == 1554

    recovered = FarmSessionTracker(
        HistoryStore(tmp_path / "farm.sqlite", NOW + 20_000),
        MarketPriceProvider(tmp_path / "market.sqlite", 12301),
        ItemCatalog(tmp_path / "items.json"),
    )
    assert recovered.apply(loot)["applied"] is False
    assert recovered.snapshot() is None


def test_negative_kina_does_not_start_and_positive_kina_stops_after_idle(tmp_path: Path):
    subject, _ = tracker(tmp_path)
    assert subject.apply(event("k-1", "kina_delta", NOW, currency="kina", delta=-10))["applied"] is False
    assert subject.snapshot() is None
    assert subject.apply(event("k-2", "kina_delta", NOW + 1, currency="kina", delta=482))["auto_started"] is True
    assert subject.snapshot()["observed_direct_kina"] == 482
    assert subject.expire(NOW + 15_000) is None
    assert subject.expire(NOW + 15_001)["status"] == "stopped"


def test_market_quote_is_exact_server_and_stale_is_unknown(tmp_path: Path):
    database = market_database(tmp_path / "market.sqlite")
    fresh = MarketPriceProvider(database, 12301, 86_400).lookup(123, NOW)
    assert fresh is not None and fresh.unit_price == 777 and fresh.market_server_id == 12301
    assert MarketPriceProvider(database, 99999, 86_400).lookup(123, NOW) is None
    assert MarketPriceProvider(database, 12301, 1).lookup(123, NOW + 2_000) is None


def test_confirmed_packet_decoder_shapes(tmp_path: Path):
    catalog = ItemCatalog(catalog_file(tmp_path / "items.json"))
    decoder = EconomyPacketDecoder("bus-1", catalog, ZoneCatalog(None))
    identity = PacketIdentity(NOW, 7, 100, 0)
    loot = bytearray(27)
    struct.pack_into("<HII", loot, 0, 0x8D33, 0, 0)
    struct.pack_into("<II", loot, 7, 123, 2)
    decoded = decoder.decode(bytes(loot), identity)
    assert decoded["kind"] == "loot" and decoded["quantity"] == 2

    inventory = bytearray(127)
    struct.pack_into("<H", inventory, 0, 0x561B)
    struct.pack_into("<II", inventory, 30, 930_100_031, 1000)
    assert decoder.decode(bytes(inventory), identity) is None
    struct.pack_into("<I", inventory, 34, 1482)
    delta = decoder.decode(bytes(inventory), PacketIdentity(NOW + 1, 7, 101, 1))
    assert delta["kind"] == "kina_delta" and delta["delta"] == 482


def test_zone_catalog_fails_closed_on_build_mismatch(tmp_path: Path):
    path = tmp_path / "zones.json"
    path.write_text(
        json.dumps({"schema": "a2.farm.zone-catalog.v1", "source_build": 10, "maps": []}),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="zone_catalog_build_mismatch"):
        ZoneCatalog(path, expected_build=11)


def test_malformed_bulk_inventory_marker_is_rejected(tmp_path: Path):
    decoder = EconomyPacketDecoder("bus-1", ItemCatalog(catalog_file(tmp_path / "items.json")))
    malformed = bytearray(127)
    struct.pack_into("<H", malformed, 0, 0x561B)
    struct.pack_into("<I", malformed, 123, 930_100_031)
    assert decoder.decode(bytes(malformed), PacketIdentity(NOW, 1, 1, 0)) is None


def test_packet_bus_round_trip_and_capture_sink(tmp_path: Path):
    writer = PacketBusWriter(tmp_path)
    writer.write(BusRecord(NOW, 1, 1, 9, 7777, 100, 200, b"payload"))
    path = writer.path
    writer.close()
    with path.open("rb") as stream:
        version = read_header(stream)
        assert version == 3
        assert read_record(stream, version).payload == b"payload"

    sink_dir = tmp_path / "sink"
    sink = CaptureBusSink(sink_dir)
    source, destination = socket.inet_aton("203.0.113.10"), socket.inet_aton("192.0.2.10")
    tcp = struct.pack("!HHIIHHHH", 7777, 50000, 1, 2, 5 << 12, 0, 0, 0) + b"game"
    ip = bytes([0x45, 0]) + struct.pack("!H", 20 + len(tcp)) + b"\0\0\0\0" + bytes([64, 6]) + b"\0\0" + source + destination
    frame = b"\0" * 12 + struct.pack("!H", 0x0800) + ip + tcp
    scope = {"tcp": [{"local": 50000, "peer": 7777, "address": "203.0.113.10"}]}
    sink.observe(NOW * 1000, frame, scope)
    sink.observe(NOW * 1000, frame, scope)
    sink.close()
    with next(sink_dir.glob("*.a2bus")).open("rb") as stream:
        version = read_header(stream)
        record = read_record(stream, version)
        assert record.direction == 1 and record.peer_port == 7777 and record.payload == b"game"
        assert read_record(stream, version) is None


def _varint(value: int) -> bytes:
    result = bytearray()
    while True:
        byte = value & 0x7F
        value >>= 7
        result.append(byte | (0x80 if value else 0))
        if not value:
            return bytes(result)


def _framed(payload: bytes) -> bytes:
    for width in range(1, 6):
        declared = width + len(payload) + 3
        prefix = _varint(declared)
        if len(prefix) == width:
            return prefix + payload
    raise AssertionError("unreachable")


def test_stream_decoder_reassembles_fragment_and_ignores_retransmit(tmp_path: Path):
    catalog = ItemCatalog(catalog_file(tmp_path / "items.json"))
    stream = EconomyStreamDecoder(EconomyPacketDecoder("bus-1", catalog))
    loot = bytearray(27)
    struct.pack_into("<H", loot, 0, 0x8D33)
    struct.pack_into("<II", loot, 7, 123, 2)
    wire = _framed(bytes(loot))
    first, second = wire[:9], wire[9:]
    assert stream.push(BusRecord(NOW, 1, 1, 1, 7777, 100, 0, first)) == []
    decoded = stream.push(BusRecord(NOW + 1, 1, 1, 1, 7777, 109, 0, second))
    assert len(decoded) == 1 and decoded[0]["kind"] == "loot"
    assert stream.push(BusRecord(NOW + 2, 1, 1, 1, 7777, 109, 0, second)) == []


def test_stream_decoder_buffers_bounded_out_of_order_segments(tmp_path: Path):
    catalog = ItemCatalog(catalog_file(tmp_path / "items.json"))
    stream = EconomyStreamDecoder(EconomyPacketDecoder("bus-1", catalog))
    loot = bytearray(27)
    struct.pack_into("<H", loot, 0, 0x8D33)
    struct.pack_into("<II", loot, 7, 123, 2)
    wire = _framed(bytes(loot))
    first, second, third = wire[:6], wire[6:15], wire[15:]
    assert stream.push(BusRecord(NOW, 1, 1, 1, 7777, 100, 0, first)) == []
    assert stream.push(BusRecord(NOW + 1, 1, 1, 1, 7777, 115, 0, third)) == []
    decoded = stream.push(BusRecord(NOW + 2, 1, 1, 1, 7777, 106, 0, second))
    assert len(decoded) == 1 and decoded[0]["kind"] == "loot"


def test_packet_bus_retention_and_segment_budget_are_bounded(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(packet_bus_module, "MAX_BUS_SEGMENTS", 2)
    first = PacketBusWriter(tmp_path)
    first_path = first.path
    first.close()
    second = PacketBusWriter(tmp_path)
    second.close()
    third = PacketBusWriter(tmp_path)
    third.close()
    assert not first_path.exists()
    assert len(list(tmp_path.glob("*.a2bus"))) == 2

    monkeypatch.setattr(packet_bus_module, "MAX_BUS_SEGMENT_BYTES", 60)
    bounded = PacketBusWriter(tmp_path / "bounded")
    bounded.write(BusRecord(NOW, 1, 1, 1, 7777, 1, 0, b"12345"))
    with pytest.raises(ValueError, match="packet_bus_segment_budget_exceeded"):
        bounded.write(BusRecord(NOW, 1, 1, 1, 7777, 6, 0, b"x"))
    bounded.close()


def test_sync_uses_exact_aggregate_contract_and_reuses_receipt(tmp_path: Path):
    subject, history = tracker(tmp_path)
    subject.apply(event("k-1", "kina_delta", NOW, currency="kina", delta=5))
    stopped = subject.stop(NOW + 1000)
    seen: list[dict[str, object]] = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            return

        def do_POST(self):
            length = int(self.headers["Content-Length"])
            payload = json.loads(self.rfile.read(length))
            seen.append({"path": self.path, "headers": self.headers, "payload": payload})
            receipt = {
                "schema": "a2.farm.sync-receipt.v1",
                "payload_id": payload["payload_id"],
                "accepted": True,
                "receipt_id": "receipt-1",
            }
            body = json.dumps(receipt).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        origin = f"http://127.0.0.1:{server.server_port}"
        receipt = sync_session(history, stopped["session_id"], origin, "write-only-token")
        reused = sync_session(history, stopped["session_id"], origin, "write-only-token")
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
    assert receipt["accepted"] is True and reused["reused"] is True
    assert len(seen) == 1
    assert seen[0]["path"] == "/api/v1/desktop/farm/sessions"
    assert seen[0]["headers"]["Idempotency-Key"] == seen[0]["payload"]["payload_id"]
    assert seen[0]["payload"]["session"]["loot"] == []


def test_history_marks_running_session_interrupted_on_restart(tmp_path: Path):
    subject, _ = tracker(tmp_path)
    subject.apply(event("k-1", "kina_delta", NOW, currency="kina", delta=1))
    reopened = HistoryStore(tmp_path / "farm.sqlite", NOW + 1)
    assert reopened.list()[0]["status"] == "interrupted"


def test_zone_event_is_durably_deduplicated(tmp_path: Path):
    subject, _ = tracker(tmp_path)
    zone = event("zone-1", "zone_map_identity", NOW, map_id=1110, label="Altgard")
    assert subject.apply(zone)["applied"] is True
    reopened = FarmSessionTracker(
        HistoryStore(tmp_path / "farm.sqlite", NOW + 1),
        MarketPriceProvider(tmp_path / "market.sqlite", 12301),
        ItemCatalog(tmp_path / "items.json"),
    )
    assert reopened.apply(zone)["applied"] is False


def test_runtime_tails_new_bus_segment_and_persists_session(tmp_path: Path):
    catalog_path = catalog_file(tmp_path / "items.json")
    database = market_database(tmp_path / "market.sqlite")
    bus = tmp_path / "packet-live"
    runtime = Runtime(
        history=tmp_path / "farm.sqlite",
        market_database=database,
        market_server_id=12301,
        catalog=catalog_path,
        zones=None,
        packet_bus=bus,
        idle_seconds=15,
        max_quote_age_seconds=86_400,
        sync_origin=None,
        sync_token=None,
    )
    runtime.start()
    time.sleep(0.02)
    writer = PacketBusWriter(bus)
    baseline = bytearray(127)
    struct.pack_into("<H", baseline, 0, 0x561B)
    struct.pack_into("<II", baseline, 20, 930_100_031, 1000)
    update = bytearray(baseline)
    struct.pack_into("<I", update, 24, 1482)
    first = _framed(bytes(baseline))
    second = _framed(bytes(update))
    writer.write(BusRecord(NOW, 1, 1, 1, 7777, 100, 0, first))
    writer.write(BusRecord(NOW + 1, 1, 1, 1, 7777, 100 + len(first), 0, second))
    writer.close()
    deadline = time.monotonic() + 3
    while runtime.tracker.snapshot() is None and time.monotonic() < deadline:
        time.sleep(0.02)
    try:
        assert runtime.tracker.snapshot()["observed_direct_kina"] == 482
        assert runtime.health()["decoded_events"] == 1
    finally:
        runtime.close()
    assert runtime.history.list()[0]["status"] == "stopped"
