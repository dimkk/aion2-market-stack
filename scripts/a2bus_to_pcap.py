#!/usr/bin/env python3
"""Convert the local A2 packet bus to a synthetic Ethernet PCAP.

The converter is intentionally transport-only.  It never prints packet bytes,
addresses from the host, credentials, or paths.  Synthetic tuples preserve the
bus flow boundary, direction, TCP sequence/acknowledgement values and capture
time so a private TLS analyzer can consume the result with the matching local
key log.

The generated PCAP is raw private material.  Keep it in the ignored private
spool and pass it only to the project private analyzer.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import struct
from typing import BinaryIO, Iterator


MAGIC = b"A2MKTAP\0"
BUS_HEADER = struct.Struct("<8sH6x")
RECORD_V2 = struct.Struct("<qB3xQH2xIII")
RECORD_V3 = struct.Struct("<qBB2xQH2xIII")
PCAP_HEADER = struct.Struct("<IHHIIII")
PCAP_RECORD = struct.Struct("<IIII")
MAX_PAYLOAD = 1_048_576
MAX_OUTPUT_BYTES = 256 * 1024 * 1024


class IncompleteLiveTail(Exception):
    pass


def checksum(data: bytes) -> int:
    if len(data) & 1:
        data += b"\0"
    total = sum(struct.unpack(f"!{len(data) // 2}H", data))
    total = (total & 0xFFFF) + (total >> 16)
    total = (total & 0xFFFF) + (total >> 16)
    return (~total) & 0xFFFF


def synthetic_tuple(flow_index: int, peer_port: int) -> tuple[bytes, bytes, int, int]:
    if not 0 <= flow_index < 60_000:
        raise ValueError("too_many_packet_bus_flows")
    local_ip = bytes((10, 255, 0, 1))
    remote_ip = bytes((10, 254, (flow_index // 250) & 0xFF, (flow_index % 250) + 1))
    local_port = 5_000 + flow_index
    return local_ip, remote_ip, local_port, peer_port


def ipv4_tcp_frame(
    *, source_ip: bytes, destination_ip: bytes, source_port: int,
    destination_port: int, sequence: int, acknowledgement: int,
    payload: bytes, identification: int, tcp_flags: int = 0x18,
) -> bytes:
    tcp_without_checksum = struct.pack(
        "!HHIIBBHHH",
        source_port,
        destination_port,
        sequence,
        acknowledgement,
        5 << 4,
        tcp_flags,
        65_535,
        0,
        0,
    )
    pseudo = source_ip + destination_ip + struct.pack(
        "!BBH", 0, 6, len(tcp_without_checksum) + len(payload)
    )
    tcp_sum = checksum(pseudo + tcp_without_checksum + payload)
    tcp = struct.pack(
        "!HHIIBBHHH",
        source_port,
        destination_port,
        sequence,
        acknowledgement,
        5 << 4,
        tcp_flags,
        65_535,
        tcp_sum,
        0,
    )
    total_length = 20 + len(tcp) + len(payload)
    ip_without_checksum = struct.pack(
        "!BBHHHBBH4s4s",
        0x45,
        0,
        total_length,
        identification & 0xFFFF,
        0x4000,
        64,
        6,
        0,
        source_ip,
        destination_ip,
    )
    ip = ip_without_checksum[:10] + struct.pack("!H", checksum(ip_without_checksum)) + ip_without_checksum[12:]
    ethernet = b"\x02\0\0\0\0\x02\x02\0\0\0\0\x01\x08\x00"
    return ethernet + ip + tcp + payload


def ipv4_udp_frame(
    *, source_ip: bytes, destination_ip: bytes, source_port: int,
    destination_port: int, payload: bytes, identification: int,
) -> bytes:
    udp_without_checksum = struct.pack(
        "!HHHH", source_port, destination_port, 8 + len(payload), 0
    )
    pseudo = source_ip + destination_ip + struct.pack(
        "!BBH", 0, 17, len(udp_without_checksum) + len(payload)
    )
    udp_sum = checksum(pseudo + udp_without_checksum + payload) or 0xFFFF
    udp = struct.pack(
        "!HHHH", source_port, destination_port, 8 + len(payload), udp_sum
    )
    total_length = 20 + len(udp) + len(payload)
    ip_without_checksum = struct.pack(
        "!BBHHHBBH4s4s",
        0x45,
        0,
        total_length,
        identification & 0xFFFF,
        0x4000,
        64,
        17,
        0,
        source_ip,
        destination_ip,
    )
    ip = ip_without_checksum[:10] + struct.pack("!H", checksum(ip_without_checksum)) + ip_without_checksum[12:]
    ethernet = b"\x02\0\0\0\0\x02\x02\0\0\0\0\x01\x08\x00"
    return ethernet + ip + udp + payload


def read_exact(stream: BinaryIO, size: int, allow_incomplete_tail: bool) -> bytes:
    value = stream.read(size)
    if len(value) == size:
        return value
    if allow_incomplete_tail:
        raise IncompleteLiveTail
    raise ValueError("truncated_packet_bus")


def iter_records(
    stream: BinaryIO, allow_incomplete_tail: bool,
) -> Iterator[tuple[int, int, int, int, int, int, int, bytes]]:
    header = stream.read(BUS_HEADER.size)
    if len(header) != BUS_HEADER.size:
        raise ValueError("incomplete_packet_bus_header")
    magic, version = BUS_HEADER.unpack(header)
    if magic != MAGIC or version not in (2, 3):
        raise ValueError("unsupported_packet_bus")
    record_struct = RECORD_V3 if version == 3 else RECORD_V2
    while True:
        raw = stream.read(record_struct.size)
        if not raw:
            return
        if len(raw) != record_struct.size:
            if allow_incomplete_tail:
                return
            raise ValueError("truncated_packet_bus_record")
        unpacked = record_struct.unpack(raw)
        if version == 3:
            captured, direction, transport, flow_id, peer_port, seq, ack, length = unpacked
        else:
            captured, direction, flow_id, peer_port, seq, ack, length = unpacked
            transport = 1
        if direction not in (1, 2) or transport not in (1, 2):
            raise ValueError("invalid_packet_bus_record")
        if not 0 < length <= MAX_PAYLOAD:
            raise ValueError("invalid_packet_bus_payload_length")
        try:
            payload = read_exact(stream, length, allow_incomplete_tail)
        except IncompleteLiveTail:
            return
        yield captured, direction, transport, flow_id, peer_port, seq, ack, payload


def convert(
    source: Path | list[Path],
    destination: Path,
    peer_port: int | None,
    selected_flows: set[int],
    allow_incomplete_tail: bool,
) -> dict[str, object]:
    sources = [source] if isinstance(source, Path) else list(source)
    if (
        not sources
        or len(sources) > 128
        or len({candidate.resolve() for candidate in sources}) != len(sources)
        or any(
            not candidate.is_file() or candidate.stat().st_size < BUS_HEADER.size
            for candidate in sources
        )
    ):
        raise ValueError("packet_bus_missing")
    destination.parent.mkdir(parents=True, exist_ok=True)
    flow_indexes: dict[int, int] = {}
    initialized_tcp_flows: set[int] = set()
    digest = hashlib.sha256()
    records = 0
    payload_bytes = 0
    output_bytes = 0
    skipped_incomplete_tail = False

    def write(output: BinaryIO, data: bytes) -> None:
        nonlocal output_bytes
        if output_bytes + len(data) > MAX_OUTPUT_BYTES:
            raise ValueError("pcap_output_too_large")
        output.write(data)
        digest.update(data)
        output_bytes += len(data)

    try:
        with destination.open("xb") as output:
            write(output, PCAP_HEADER.pack(0xA1B2C3D4, 2, 4, 0, 0, 65_535, 1))
            for source_index, candidate in enumerate(sources):
                # Only the final segment can be actively appended.  A short
                # record in an older segment is corruption, not a live tail.
                segment_allows_incomplete_tail = (
                    allow_incomplete_tail and source_index == len(sources) - 1
                )
                with candidate.open("rb") as stream:
                    iterator = iter_records(stream, segment_allows_incomplete_tail)
                    for captured, direction, transport, flow_id, remote_port, seq, ack, payload in iterator:
                        if peer_port is not None and remote_port != peer_port:
                            continue
                        if selected_flows and flow_id not in selected_flows:
                            continue
                        flow_index = flow_indexes.setdefault(flow_id, len(flow_indexes))
                        local_ip, remote_ip, local_port, remote_port = synthetic_tuple(
                            flow_index, remote_port
                        )
                        if transport == 1 and flow_id not in initialized_tcp_flows:
                            client_sequence = seq if direction == 2 else ack
                            server_sequence = ack if direction == 2 else seq
                            handshake = (
                                (captured - 3, local_ip, remote_ip, local_port, remote_port,
                                 (client_sequence - 1) & 0xFFFFFFFF, 0, 0x02),
                                (captured - 2, remote_ip, local_ip, remote_port, local_port,
                                 (server_sequence - 1) & 0xFFFFFFFF, client_sequence, 0x12),
                                (captured - 1, local_ip, remote_ip, local_port, remote_port,
                                 client_sequence, server_sequence, 0x10),
                            )
                            for (synthetic_time, syn_source, syn_destination,
                                 syn_source_port, syn_destination_port, syn_sequence,
                                 syn_acknowledgement, syn_flags) in handshake:
                                syn_frame = ipv4_tcp_frame(
                                    source_ip=syn_source,
                                    destination_ip=syn_destination,
                                    source_port=syn_source_port,
                                    destination_port=syn_destination_port,
                                    sequence=syn_sequence,
                                    acknowledgement=syn_acknowledgement,
                                    payload=b"",
                                    identification=records,
                                    tcp_flags=syn_flags,
                                )
                                seconds, milliseconds = divmod(synthetic_time, 1_000)
                                write(output, PCAP_RECORD.pack(
                                    seconds, milliseconds * 1_000,
                                    len(syn_frame), len(syn_frame),
                                ))
                                write(output, syn_frame)
                                records += 1
                            initialized_tcp_flows.add(flow_id)
                        if direction == 2:
                            source_ip, destination_ip = local_ip, remote_ip
                            source_port, destination_port = local_port, remote_port
                        else:
                            source_ip, destination_ip = remote_ip, local_ip
                            source_port, destination_port = remote_port, local_port
                        if transport == 1:
                            frame = ipv4_tcp_frame(
                                source_ip=source_ip,
                                destination_ip=destination_ip,
                                source_port=source_port,
                                destination_port=destination_port,
                                sequence=seq,
                                acknowledgement=ack,
                                payload=payload,
                                identification=records,
                            )
                        else:
                            frame = ipv4_udp_frame(
                                source_ip=source_ip,
                                destination_ip=destination_ip,
                                source_port=source_port,
                                destination_port=destination_port,
                                payload=payload,
                                identification=records,
                            )
                        seconds, milliseconds = divmod(captured, 1_000)
                        write(output, PCAP_RECORD.pack(
                            seconds, milliseconds * 1_000, len(frame), len(frame)
                        ))
                        write(output, frame)
                        records += 1
                        payload_bytes += len(payload)
            output.flush()
    except Exception:
        destination.unlink(missing_ok=True)
        raise

    if records == 0:
        destination.unlink(missing_ok=True)
        raise ValueError("no_matching_packet_bus_records")
    return {
        "status": "pcap_created_private",
        "format": "pcap-ethernet-ipv4-synthetic-tuples-v2",
        "records": records,
        "flows": len(flow_indexes),
        "synthetic_tcp_handshake_records": len(initialized_tcp_flows) * 3,
        "payload_bytes": payload_bytes,
        "pcap_bytes": output_bytes,
        "pcap_sha256": digest.hexdigest(),
        "source_segments": len(sources),
        "incomplete_live_tail_skipped": skipped_incomplete_tail,
        "raw_exposed": False,
        "paths_exposed": False,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("source", type=Path)
    parser.add_argument("destination", type=Path)
    parser.add_argument("--additional-source", action="append", type=Path, default=[])
    parser.add_argument("--peer-port", type=int)
    parser.add_argument("--flow-id", action="append", type=int, default=[])
    parser.add_argument("--allow-incomplete-tail", action="store_true")
    args = parser.parse_args()
    if args.peer_port is not None and not 1 <= args.peer_port <= 65_535:
        raise SystemExit("invalid_peer_port")
    try:
        receipt = convert(
            [args.source, *args.additional_source],
            args.destination,
            args.peer_port,
            set(args.flow_id),
            args.allow_incomplete_tail,
        )
    except (OSError, ValueError) as error:
        print(json.dumps({
            "status": "pcap_conversion_failed",
            "error": str(error),
            "raw_exposed": False,
            "paths_exposed": False,
        }, sort_keys=True))
        return 1
    print(json.dumps(receipt, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
