#!/usr/bin/env python3
"""Standalone Windows collector for the public self-hosted distribution.

The collector is deliberately independent from A2Tools. It uses the project-owned
Npcap recorder, bundles one bounded process-scoped capture with an NSS TLS keylog,
and atomically drops the private bundle into the local Docker worker inbox.

The bundle is sensitive session material. It is never printed, committed, or sent
to a remote service by this program.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import tempfile
import time
import zipfile


ROOT = Path(__file__).resolve().parents[2]
KEYLOG_LINE = re.compile(
    rb"^(?:(?:CLIENT_RANDOM|CLIENT_EARLY_TRAFFIC_SECRET|CLIENT_HANDSHAKE_TRAFFIC_SECRET|"
    rb"SERVER_HANDSHAKE_TRAFFIC_SECRET|CLIENT_TRAFFIC_SECRET_[0-9]+|"
    rb"SERVER_TRAFFIC_SECRET_[0-9]+|EXPORTER_SECRET) [0-9A-Fa-f]+ [0-9A-Fa-f]+|"
    rb"RSA [0-9A-Fa-f]{16} [0-9A-Fa-f]{96})$"
)
MAX_KEYLOG_BYTES = 16 * 1024 * 1024
MAX_CAPTURE_BYTES = 96 * 1024 * 1024
SAFE_ERRORS = frozenset(
    {
        "capture_artifact_not_unique",
        "capture_bundle_write_failed",
        "capture_interval_invalid",
        "collector_paths_overlap",
        "tls_keylog_invalid",
        "tls_keylog_missing",
    }
)


def _load_recorder():
    path = ROOT / "scripts" / "capture_npcap_once.py"
    spec = importlib.util.spec_from_file_location("aion2_public_npcap", path)
    if spec is None or spec.loader is None:
        raise ValueError("capture_bundle_write_failed")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _validate_keylog(path: Path) -> bytes:
    if not path.is_file() or path.is_symlink():
        raise ValueError("tls_keylog_missing")
    data = path.read_bytes()
    if not 0 < len(data) <= MAX_KEYLOG_BYTES:
        raise ValueError("tls_keylog_invalid")
    lines = [line.strip() for line in data.splitlines() if line.strip()]
    if not lines or any(KEYLOG_LINE.fullmatch(line) is None for line in lines):
        raise ValueError("tls_keylog_invalid")
    return b"\n".join(lines) + b"\n"


def build_bundle(capture: Path, keylog: Path, inbox: Path) -> dict[str, object]:
    """Create one atomic bundle and return a credential-free receipt."""
    capture = capture.resolve(strict=True)
    keylog = keylog.resolve(strict=True)
    inbox = inbox.resolve(strict=True)
    if inbox in capture.parents or inbox in keylog.parents or capture.parent == inbox:
        raise ValueError("collector_paths_overlap")
    if capture.is_symlink() or not 0 < capture.stat().st_size <= MAX_CAPTURE_BYTES:
        raise ValueError("capture_bundle_write_failed")
    keylog_bytes = _validate_keylog(keylog)
    capture_digest = hashlib.sha256(capture.read_bytes()).hexdigest()
    bundle_name = f"capture-{time.time_ns()}-{capture_digest[:12]}.a2tlsbundle"
    temporary: Path | None = None
    try:
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=".capture-", suffix=".partial", dir=inbox
        )
        os.close(descriptor)
        temporary = Path(temporary_name)
        with zipfile.ZipFile(
            temporary, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=1
        ) as archive:
            archive.write(capture, "capture.pcapng")
            archive.writestr("tls.keys", keylog_bytes)
        with temporary.open("rb+") as stream:
            os.fsync(stream.fileno())
        destination = inbox / bundle_name
        os.replace(temporary, destination)
        temporary = None
        return {
            "status": "bundle_ready",
            "capture_sha256": capture_digest,
            "bundle_sha256": hashlib.sha256(destination.read_bytes()).hexdigest(),
            "size_bytes": destination.stat().st_size,
            "raw_exposed": False,
            "paths_exposed": False,
        }
    except OSError as error:
        raise ValueError("capture_bundle_write_failed") from error
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def capture_once(
    *, spool: Path, inbox: Path, keylog: Path, seconds: int, hosts: list[str]
) -> dict[str, object]:
    spool.mkdir(parents=True, exist_ok=True)
    inbox.mkdir(parents=True, exist_ok=True)
    before = {path.resolve() for path in spool.glob("npcap-*.pcapng")}
    recorder = _load_recorder()
    recorder.PRIVATE_ROOT = spool
    recorder.capture("game", seconds, hosts)
    created = [
        path for path in spool.glob("npcap-*.pcapng") if path.resolve() not in before
    ]
    if len(created) != 1:
        raise ValueError("capture_artifact_not_unique")
    capture = created[0]
    try:
        return build_bundle(capture, keylog, inbox)
    finally:
        capture.unlink(missing_ok=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--spool", type=Path, required=True)
    parser.add_argument("--inbox", type=Path, required=True)
    parser.add_argument("--keylog", type=Path, required=True)
    parser.add_argument("--seconds", type=int, default=120)
    parser.add_argument("--interval", type=int, default=5)
    parser.add_argument("--host", action="append", default=[])
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()
    if not 10 <= args.seconds <= 300 or not 1 <= args.interval <= 300:
        print(json.dumps({"status": "collector_failed", "error": "capture_interval_invalid"}))
        return 2
    try:
        recorder_errors = getattr(_load_recorder(), "SAFE_ERROR_CODES", set())
    except (OSError, ValueError):
        recorder_errors = set()
    while True:
        try:
            receipt = capture_once(
                spool=args.spool.resolve(),
                inbox=args.inbox.resolve(),
                keylog=args.keylog.resolve(),
                seconds=args.seconds,
                hosts=args.host,
            )
            print(json.dumps(receipt, sort_keys=True), flush=True)
        except KeyboardInterrupt:
            return 0
        except (OSError, ValueError) as error:
            code = str(error)
            if code not in SAFE_ERRORS and code not in recorder_errors:
                code = "collector_failed"
            print(
                json.dumps(
                    {
                        "status": "collector_waiting",
                        "error": code,
                        "raw_exposed": False,
                        "paths_exposed": False,
                    },
                    sort_keys=True,
                ),
                flush=True,
            )
        if args.once:
            return 0
        time.sleep(args.interval)


if __name__ == "__main__":
    raise SystemExit(main())
