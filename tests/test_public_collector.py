import importlib.util
from pathlib import Path
import socket
import struct
import zipfile

from tools.farm_tracker.packet_bus import read_header, read_record


ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location(
    "public_collector", ROOT / "tools/public_collector/collector.py"
)
collector = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(collector)


def test_build_bundle_has_only_fixed_private_members(tmp_path: Path) -> None:
    raw = tmp_path / "raw"
    inbox = tmp_path / "inbox"
    raw.mkdir()
    inbox.mkdir()
    capture = raw / "capture.pcapng"
    capture.write_bytes(b"synthetic-pcap")
    keylog = raw / "tls.keys"
    keylog.write_text("CLIENT_RANDOM " + "A" * 64 + " " + "B" * 96 + "\n", encoding="ascii")

    receipt = collector.build_bundle(capture, keylog, inbox)

    assert receipt["status"] == "bundle_ready"
    assert receipt["raw_exposed"] is False
    bundle = next(inbox.glob("*.a2tlsbundle"))
    with zipfile.ZipFile(bundle) as archive:
        assert archive.namelist() == ["capture.pcapng", "tls.keys"]
        assert archive.read("capture.pcapng") == b"synthetic-pcap"


def test_build_bundle_rejects_unrecognized_keylog_line(tmp_path: Path) -> None:
    raw = tmp_path / "raw"
    inbox = tmp_path / "inbox"
    raw.mkdir()
    inbox.mkdir()
    capture = raw / "capture.pcapng"
    capture.write_bytes(b"synthetic-pcap")
    keylog = raw / "tls.keys"
    keylog.write_text("Authorization: secret\n", encoding="ascii")

    try:
        collector.build_bundle(capture, keylog, inbox)
    except ValueError as error:
        assert str(error) == "tls_keylog_invalid"
    else:
        raise AssertionError("unsafe keylog accepted")


def test_one_collector_capture_also_writes_packet_bus(tmp_path: Path, monkeypatch) -> None:
    spool, inbox, bus = tmp_path / "spool", tmp_path / "inbox", tmp_path / "bus"
    keylog = tmp_path / "tls.keys"
    keylog.write_text("CLIENT_RANDOM " + "A" * 64 + " " + "B" * 96 + "\n", encoding="ascii")

    source, destination = socket.inet_aton("203.0.113.10"), socket.inet_aton("192.0.2.10")
    tcp = struct.pack("!HHIIHHHH", 7777, 50000, 1, 2, 5 << 12, 0, 0, 0) + b"game"
    ip = bytes([0x45, 0]) + struct.pack("!H", 20 + len(tcp)) + b"\0\0\0\0" + bytes([64, 6]) + b"\0\0" + source + destination
    frame = b"\0" * 12 + struct.pack("!H", 0x0800) + ip + tcp

    class Recorder:
        PRIVATE_ROOT = None

        @staticmethod
        def capture(mode, seconds, hosts, observer):
            assert mode == "game" and seconds == 10 and hosts == [] and observer is not None
            (Recorder.PRIVATE_ROOT / "npcap-test.pcapng").write_bytes(b"synthetic-pcap")
            observer(
                1_790_000_000_000_000,
                frame,
                {"tcp": [{"local": 50000, "peer": 7777, "address": "203.0.113.10"}]},
            )

    monkeypatch.setattr(collector, "_load_recorder", lambda: Recorder)
    receipt = collector.capture_once(
        spool=spool,
        inbox=inbox,
        keylog=keylog,
        seconds=10,
        hosts=[],
        packet_bus_dir=bus,
    )
    assert receipt["status"] == "bundle_ready"
    with next(bus.glob("*.a2bus")).open("rb") as stream:
        version = read_header(stream)
        record = read_record(stream, version)
    assert record.payload == b"game" and record.direction == 1


def test_packet_bus_failure_does_not_abort_market_bundle(tmp_path: Path, monkeypatch) -> None:
    spool, inbox = tmp_path / "spool", tmp_path / "inbox"
    keylog = tmp_path / "tls.keys"
    keylog.write_text("CLIENT_RANDOM " + "A" * 64 + " " + "B" * 96 + "\n", encoding="ascii")

    class Recorder:
        PRIVATE_ROOT = None

        @staticmethod
        def capture(_mode, _seconds, _hosts, observer):
            (Recorder.PRIVATE_ROOT / "npcap-test.pcapng").write_bytes(b"synthetic-pcap")
            observer(1, b"ignored", {})

    class BrokenSink:
        def __init__(self, _path):
            raise OSError("private detail must not escape")

    monkeypatch.setattr(collector, "_load_recorder", lambda: Recorder)
    monkeypatch.setattr(collector, "CaptureBusSink", BrokenSink)
    receipt = collector.capture_once(
        spool=spool,
        inbox=inbox,
        keylog=keylog,
        seconds=10,
        hosts=[],
        packet_bus_dir=tmp_path / "bus",
    )
    assert receipt["status"] == "bundle_ready"
    assert receipt["packet_bus_status"] == "initialization_failed"
