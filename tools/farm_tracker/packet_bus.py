"""Project-owned A2 packet-bus codec, stream reassembly and safe live tail."""

from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass
import hashlib
import ipaddress
import os
from pathlib import Path
import socket
import struct
import threading
import time
import uuid
from typing import Any, BinaryIO, Callable

from .decoder import EconomyPacketDecoder, PacketIdentity


MAGIC = b"A2MKTAP\0"
VERSION = 3
HEADER = MAGIC + struct.pack("<H6x", VERSION)
MAX_PAYLOAD = 1_048_576
MAX_BUFFER = 2 * 1024 * 1024
MAX_FLOWS = 256
MAX_REORDER_SEGMENTS = 64
MAX_REORDER_BYTES = 512 * 1024
MAX_REORDER_GAP = 512 * 1024
MAX_BUS_SEGMENT_BYTES = 32 * 1024 * 1024
MAX_BUS_SEGMENTS = 8


@dataclass(frozen=True)
class BusRecord:
    captured_at_ms: int
    direction: int
    transport: int
    flow_id: int
    peer_port: int
    tcp_seq: int
    tcp_ack: int
    payload: bytes


def write_record(stream: BinaryIO, record: BusRecord) -> None:
    if not 0 < len(record.payload) <= MAX_PAYLOAD:
        raise ValueError("packet_bus_payload_invalid")
    stream.write(struct.pack(
        "<qBB2xQH2xIII", record.captured_at_ms, record.direction, record.transport,
        record.flow_id, record.peer_port, record.tcp_seq, record.tcp_ack, len(record.payload),
    ))
    stream.write(record.payload)
    stream.flush()


def read_header(stream: BinaryIO) -> int:
    raw = stream.read(16)
    if len(raw) != 16 or raw[:8] != MAGIC:
        raise ValueError("packet_bus_header_invalid")
    version = struct.unpack_from("<H", raw, 8)[0]
    if version not in {1, 2, 3}:
        raise ValueError("packet_bus_version_invalid")
    return version


def read_record(stream: BinaryIO, version: int, *, allow_incomplete: bool = False) -> BusRecord | None:
    start = stream.tell()
    header_size = 36 if version >= 2 else 24
    raw = stream.read(header_size)
    if not raw:
        return None
    if len(raw) != header_size:
        if allow_incomplete:
            stream.seek(start)
            return None
        raise ValueError("packet_bus_record_truncated")
    captured_at_ms = struct.unpack_from("<q", raw)[0]
    direction = raw[8]
    transport = raw[9] if version >= 3 else 1
    if version >= 2:
        flow_id = struct.unpack_from("<Q", raw, 12)[0]
        peer_port = struct.unpack_from("<H", raw, 20)[0]
        tcp_seq, tcp_ack, length = struct.unpack_from("<III", raw, 24)
    else:
        flow_id = peer_port = 0
        tcp_seq, tcp_ack, length = struct.unpack_from("<III", raw, 12)
    if direction not in {1, 2} or transport not in {1, 2} or not 0 < length <= MAX_PAYLOAD:
        raise ValueError("packet_bus_record_invalid")
    payload = stream.read(length)
    if len(payload) != length:
        if allow_incomplete:
            stream.seek(start)
            return None
        raise ValueError("packet_bus_payload_truncated")
    return BusRecord(captured_at_ms, direction, transport, flow_id, peer_port, tcp_seq, tcp_ack, payload)


class PacketBusWriter:
    def __init__(self, directory: Path) -> None:
        directory.mkdir(parents=True, exist_ok=True)
        if directory.is_symlink():
            raise ValueError("packet_bus_directory_invalid")
        existing = sorted(
            (
                path for path in directory.glob("*.a2bus")
                if path.is_file() and not path.is_symlink()
            ),
            key=lambda path: (path.stat().st_mtime_ns, path.name),
        )
        while len(existing) >= MAX_BUS_SEGMENTS:
            try:
                existing.pop(0).unlink()
            except OSError as error:
                raise ValueError("packet_bus_retention_unavailable") from error
        self.path = directory / f"packet-{time.time_ns()}-{os.getpid()}-{uuid.uuid4().hex[:12]}.a2bus"
        self.stream = self.path.open("xb", buffering=0)
        self.stream.write(HEADER)

    def write(self, record: BusRecord) -> None:
        if self.stream.tell() + 36 + len(record.payload) > MAX_BUS_SEGMENT_BYTES:
            raise ValueError("packet_bus_segment_budget_exceeded")
        write_record(self.stream, record)

    def close(self) -> None:
        self.stream.close()


def _read_varint(data: bytes, offset: int) -> tuple[int, int] | None:
    value = shift = 0
    for count in range(5):
        if offset + count >= len(data):
            return None
        byte = data[offset + count]
        value |= (byte & 0x7F) << shift
        if byte & 0x80 == 0:
            return value, count + 1
        shift += 7
    return None


def _walk(data: bytes, *, inner: bool = False) -> tuple[list[tuple[bool, bytes]], int]:
    frames: list[tuple[bool, bytes]] = []
    offset = 0
    while offset < len(data):
        if data[offset] == 0:
            offset += 1
            continue
        length = _read_varint(data, offset)
        if length is None or length[0] <= 3:
            if inner:
                break
            if offset + 5 > len(data):
                break
            offset += 1
            continue
        total = length[0] - 3
        if total <= 0 or total > 65_535:
            if inner:
                break
            offset += 1
            continue
        end = offset + total
        if end > len(data):
            if not inner and total > 16_384:
                offset += 1
                continue
            break
        payload_at = offset + length[1]
        bundle = payload_at + 1 < end and data[payload_at:payload_at + 2] == b"\xff\xff"
        if bundle and not inner:
            end += 1
            if end > len(data):
                break
        frames.append((bundle, data[payload_at:end]))
        offset = end
    return frames, offset


def _lz4_block(data: bytes, expected: int) -> bytes:
    if not 0 < expected <= 1_000_000:
        raise ValueError("lz4_size_invalid")
    output = bytearray()
    cursor = 0
    while cursor < len(data):
        token = data[cursor]
        cursor += 1
        literal_length = token >> 4
        if literal_length == 15:
            while True:
                if cursor >= len(data):
                    raise ValueError("lz4_truncated")
                extra = data[cursor]
                cursor += 1
                literal_length += extra
                if extra != 255:
                    break
        if cursor + literal_length > len(data) or len(output) + literal_length > expected:
            raise ValueError("lz4_truncated")
        output.extend(data[cursor:cursor + literal_length])
        cursor += literal_length
        if cursor == len(data):
            break
        if cursor + 2 > len(data):
            raise ValueError("lz4_truncated")
        distance = struct.unpack_from("<H", data, cursor)[0]
        cursor += 2
        if distance == 0 or distance > len(output):
            raise ValueError("lz4_distance_invalid")
        match_length = (token & 0x0F) + 4
        if token & 0x0F == 15:
            while True:
                if cursor >= len(data):
                    raise ValueError("lz4_truncated")
                extra = data[cursor]
                cursor += 1
                match_length += extra
                if extra != 255:
                    break
        if len(output) + match_length > expected:
            raise ValueError("lz4_size_invalid")
        for _ in range(match_length):
            output.append(output[-distance])
    if len(output) != expected:
        raise ValueError("lz4_size_invalid")
    return bytes(output)


class EconomyStreamDecoder:
    def __init__(self, decoder: EconomyPacketDecoder) -> None:
        self.decoder = decoder
        self.buffers: OrderedDict[
            int, tuple[bytearray, int | None, int | None, dict[int, bytes]]
        ] = OrderedDict()
        self.last_heartbeat_ms: int | None = None

    def _store(
        self,
        flow_id: int,
        buffer: bytearray,
        start_seq: int | None,
        next_seq: int | None,
        pending: dict[int, bytes],
    ) -> None:
        self.buffers[flow_id] = (buffer, start_seq, next_seq, pending)
        while len(self.buffers) > MAX_FLOWS:
            evicted, _ = self.buffers.popitem(last=False)
            self.decoder.forget_flow(evicted)

    @staticmethod
    def _drain_pending(buffer: bytearray, next_seq: int, pending: dict[int, bytes]) -> int:
        while pending:
            progressed = False
            for sequence, payload in list(pending.items()):
                delta = (sequence - next_seq) & 0xFFFFFFFF
                if delta == 0:
                    pending.pop(sequence)
                    buffer.extend(payload)
                    next_seq = (next_seq + len(payload)) & 0xFFFFFFFF
                    progressed = True
                    break
                if delta >= 0x80000000:
                    behind = (next_seq - sequence) & 0xFFFFFFFF
                    pending.pop(sequence)
                    if behind < len(payload):
                        tail = payload[behind:]
                        buffer.extend(tail)
                        next_seq = (next_seq + len(tail)) & 0xFFFFFFFF
                    progressed = True
                    break
            if not progressed:
                break
        return next_seq

    def push(self, record: BusRecord) -> list[dict[str, Any]]:
        if record.direction != 1 or record.transport != 1 or record.peer_port == 443:
            return []
        state = self.buffers.pop(record.flow_id, (bytearray(), None, None, {}))
        buffer, start_seq, next_seq, pending = state
        payload = record.payload
        if next_seq is None:
            start_seq = record.tcp_seq
            buffer.extend(payload)
            next_seq = (record.tcp_seq + len(payload)) & 0xFFFFFFFF
        else:
            delta = (record.tcp_seq - next_seq) & 0xFFFFFFFF
            if delta == 0:
                if not buffer:
                    start_seq = record.tcp_seq
                buffer.extend(payload)
                next_seq = (next_seq + len(payload)) & 0xFFFFFFFF
            elif delta >= 0x80000000:
                behind = (next_seq - record.tcp_seq) & 0xFFFFFFFF
                if behind >= len(payload):
                    self._store(record.flow_id, buffer, start_seq, next_seq, pending)
                    return []
                if not buffer:
                    start_seq = next_seq
                tail = payload[behind:]
                buffer.extend(tail)
                next_seq = (next_seq + len(tail)) & 0xFFFFFFFF
            else:
                pending_bytes = sum(len(item) for item in pending.values())
                if (
                    delta <= MAX_REORDER_GAP
                    and len(pending) < MAX_REORDER_SEGMENTS
                    and pending_bytes + len(payload) <= MAX_REORDER_BYTES
                ):
                    current = pending.get(record.tcp_seq)
                    if current is None or len(payload) > len(current):
                        pending[record.tcp_seq] = payload
                    self._store(record.flow_id, buffer, start_seq, next_seq, pending)
                    return []
                buffer, start_seq, pending = bytearray(payload), record.tcp_seq, {}
                next_seq = (record.tcp_seq + len(payload)) & 0xFFFFFFFF
                self.decoder.forget_flow(record.flow_id)
        next_seq = self._drain_pending(buffer, next_seq, pending)
        frames, consumed = _walk(bytes(buffer))
        events: list[dict[str, Any]] = []
        frame_index = 0
        for bundle, payload in frames:
            payloads = [payload]
            if bundle:
                try:
                    expected = struct.unpack_from("<I", payload, 2)[0]
                    expanded = _lz4_block(payload[6:], expected)
                    payloads = [inner for nested, inner in _walk(expanded, inner=True)[0] if not nested]
                except (ValueError, struct.error):
                    payloads = []
            for item in payloads:
                identity = PacketIdentity(record.captured_at_ms, record.flow_id, int(start_seq or record.tcp_seq), frame_index)
                event = self.decoder.decode(item, identity)
                if event is not None:
                    events.append(event)
                frame_index += 1
        if consumed:
            del buffer[:consumed]
            if start_seq is not None:
                start_seq = (start_seq + consumed) & 0xFFFFFFFF
        if len(buffer) > MAX_BUFFER:
            buffer, start_seq, next_seq, pending = bytearray(), None, None, {}
            self.decoder.forget_flow(record.flow_id)
        self._store(record.flow_id, buffer, start_seq, next_seq, pending)
        heartbeat_due = self.last_heartbeat_ms is None or record.captured_at_ms - self.last_heartbeat_ms >= 5_000
        if events:
            self.last_heartbeat_ms = record.captured_at_ms
        elif frames and heartbeat_due:
            events.append(self.decoder.heartbeat(PacketIdentity(record.captured_at_ms, record.flow_id, record.tcp_seq, frame_index)))
            self.last_heartbeat_ms = record.captured_at_ms
        return events


def tail_directory(
    directory: Path,
    consume: Callable[[BusRecord], None],
    stop: threading.Event,
    *,
    poll_seconds: float = 0.2,
) -> None:
    """Tail only bytes appended after startup and verified new segments."""
    directory.mkdir(parents=True, exist_ok=True)
    started_ns = time.time_ns()
    offsets: dict[Path, int] = {}
    while not stop.is_set():
        candidates = sorted(directory.glob("*.a2bus"), key=lambda path: (path.stat().st_mtime_ns, path.name))
        for path in candidates:
            if path.is_symlink() or not path.is_file():
                continue
            first = path not in offsets
            try:
                with path.open("rb") as stream:
                    version = read_header(stream)
                    if first:
                        offsets[path] = 16 if path.stat().st_ctime_ns >= started_ns else path.stat().st_size
                    stream.seek(offsets[path])
                    while True:
                        record = read_record(stream, version, allow_incomplete=True)
                        if record is None:
                            break
                        offsets[path] = stream.tell()
                        consume(record)
            except (OSError, ValueError):
                continue
        stop.wait(poll_seconds)


def _tcp_packet(frame: bytes) -> tuple[str, str, int, int, int, int, bytes] | None:
    if len(frame) < 14:
        return None
    ether_type = struct.unpack_from("!H", frame, 12)[0]
    if ether_type == 0x0800:
        if len(frame) < 34 or frame[23] != socket.IPPROTO_TCP:
            return None
        ip_start, ip_length = 14, (frame[14] & 0x0F) * 4
        total_length = struct.unpack_from("!H", frame, 16)[0]
        fragment = struct.unpack_from("!H", frame, 20)[0]
        if ip_length < 20 or total_length < ip_length + 20 or ip_start + total_length > len(frame) or fragment & 0x3FFF:
            return None
        packet_end = ip_start + total_length
        source = str(ipaddress.ip_address(frame[26:30]))
        destination = str(ipaddress.ip_address(frame[30:34]))
    elif ether_type == 0x86DD:
        if len(frame) < 54 or frame[20] != socket.IPPROTO_TCP:
            return None
        ip_start, ip_length = 14, 40
        payload_length = struct.unpack_from("!H", frame, 18)[0]
        if payload_length < 20 or ip_start + ip_length + payload_length > len(frame):
            return None
        packet_end = ip_start + ip_length + payload_length
        source = str(ipaddress.ip_address(frame[22:38]))
        destination = str(ipaddress.ip_address(frame[38:54]))
    else:
        return None
    tcp = ip_start + ip_length
    if tcp + 20 > len(frame):
        return None
    source_port, destination_port, seq, ack = struct.unpack_from("!HHII", frame, tcp)
    data_offset = (frame[tcp + 12] >> 4) * 4
    if data_offset < 20 or tcp + data_offset > len(frame):
        return None
    return source, destination, source_port, destination_port, seq, ack, frame[tcp + data_offset:packet_end]


class CaptureBusSink:
    """Convert process-scoped normalized Ethernet frames into one packet bus."""

    def __init__(self, directory: Path) -> None:
        self.writer = PacketBusWriter(directory)
        self.flows: dict[tuple[str, int, int], int] = {}
        self.seen: OrderedDict[bytes, None] = OrderedDict()

    def observe(self, captured_at_us: int, frame: bytes, scope: dict[str, Any]) -> None:
        parsed = _tcp_packet(frame)
        if parsed is None or not parsed[-1]:
            return
        source, destination, source_port, destination_port, seq, ack, payload = parsed
        match: tuple[int, int, str, int] | None = None
        tcp_rows = scope.get("tcp") or []
        if isinstance(tcp_rows, dict):
            tcp_rows = [tcp_rows]
        for row in tcp_rows:
            if not isinstance(row, dict):
                continue
            local, peer, address = row.get("local"), row.get("peer"), str(row.get("address", ""))
            if source_port == peer and destination_port == local and (not address or source == address):
                match = 1, int(peer), source, int(local)
                break
            if source_port == local and destination_port == peer and (not address or destination == address):
                match = 2, int(peer), destination, int(local)
                break
        if match is None:
            return
        direction, peer_port, remote, local_port = match
        key = (remote, local_port, peer_port)
        flow_id = self.flows.setdefault(key, len(self.flows) + 1)
        fingerprint = hashlib.blake2s(
            struct.pack("<QBI", flow_id, direction, seq) + payload,
            digest_size=16,
        ).digest()
        if fingerprint in self.seen:
            self.seen.move_to_end(fingerprint)
            return
        self.seen[fingerprint] = None
        while len(self.seen) > 8192:
            self.seen.popitem(last=False)
        self.writer.write(BusRecord(captured_at_us // 1000, direction, 1, flow_id, peer_port, seq, ack, payload))

    def close(self) -> None:
        self.writer.close()
