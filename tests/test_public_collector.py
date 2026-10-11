import importlib.util
from pathlib import Path
import zipfile


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
