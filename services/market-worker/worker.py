#!/usr/bin/env python3
"""Local self-hosted analyzer/crawler worker.

It consumes private capture bundles from a mounted inbox, decrypts only enough
traffic to recover the current read-only market session, keeps session headers
in memory, scans the build-scoped market registry, updates SQLite history, and
atomically publishes sanitized Economy views to the shared API snapshot volume.
"""

from __future__ import annotations

import hashlib
import http.client
import json
import os
from pathlib import Path
import shutil
import ssl
import sys
import tempfile
import time
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any


ROOT = Path(os.environ.get("AION2_APP_ROOT", "/opt/aion2"))
for module_root in (ROOT / "scripts", ROOT / "services" / "economy-api"):
    sys.path.insert(0, str(module_root))

import build_economy_views  # noqa: E402
import export_economy_snapshot  # noqa: E402
import market_history  # noqa: E402
import market_summary_probe  # noqa: E402
from economy_api.filesystem_store import FileSystemEconomyStore  # noqa: E402


def _now() -> datetime:
    return datetime.now(UTC)


def _iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _direct_post(headers: dict[str, str], body: bytes) -> tuple[int, dict[str, str], bytes]:
    """Send only the confirmed read-only summary request to the exact authority."""
    connection = http.client.HTTPSConnection(
        market_summary_probe.AUTHORITY,
        443,
        timeout=20,
        context=ssl.create_default_context(),
    )
    try:
        connection.request(
            market_summary_probe.METHOD,
            market_summary_probe.PATH,
            body=body,
            headers=headers,
        )
        response = connection.getresponse()
        data = response.read(market_summary_probe.listing_probe.MAX_RESPONSE_BYTES + 1)
        if len(data) > market_summary_probe.listing_probe.MAX_RESPONSE_BYTES:
            raise ValueError("market_session_response_too_large")
        response_headers = {
            name.lower(): value for name, value in response.getheaders()
        }
        return response.status, response_headers, market_summary_probe.listing_probe._decode_response(
            data, response_headers.get("content-encoding", "")
        )
    except (OSError, http.client.HTTPException, ssl.SSLError) as error:
        raise ValueError("market_session_network_failed") from error
    finally:
        connection.close()


class Config:
    def __init__(self) -> None:
        self.inbox = Path(os.environ.get("AION2_INBOX", "/var/lib/aion2/inbox"))
        self.state = Path(os.environ.get("AION2_STATE", "/var/lib/aion2/state"))
        self.snapshots = Path(
            os.environ.get("AION2_SNAPSHOTS", "/var/lib/aion2/snapshots")
        )
        self.target_batch = ROOT / os.environ.get(
            "AION2_TARGET_BATCH", "configs/global-market-summary-targets-v1.json"
        )
        self.catalog = ROOT / "exports/economy/item-catalog.json"
        self.recipes = ROOT / "exports/economy/recipes.json"
        self.rules = ROOT / "configs/global-market-rules.json"
        self.dimensions = ROOT / "configs/global-market-dimensions.json"
        self.icons = ROOT / "exports/economy/item-icons.json"
        self.database = self.state / "market-history.sqlite"
        self.status = self.state / "worker-status.json"
        self.interval = int(os.environ.get("AION2_SCAN_INTERVAL_SECONDS", "3600"))
        self.session_max_age = int(os.environ.get("AION2_SESSION_MAX_AGE_SECONDS", "900"))
        self.poll_seconds = float(os.environ.get("AION2_WORKER_POLL_SECONDS", "2"))
        self.build = int(os.environ.get("AION2_CLIENT_BUILD", "25650019"))
        if not 60 <= self.interval <= 86400 or not 60 <= self.session_max_age <= 3600:
            raise ValueError("worker_configuration_invalid")


def _write_json_atomic(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    encoded = (json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")
    descriptor, name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _initialize_database(config: Config) -> None:
    config.state.mkdir(parents=True, exist_ok=True)
    if not config.database.exists():
        market_history.import_catalog(
            config.catalog, config.database, config.build, config.build
        )


def _publish(config: Config, report: dict[str, Any], observed_at: datetime) -> dict[str, Any]:
    with tempfile.TemporaryDirectory(prefix="aion2-safe-report-", dir=config.state) as name:
        report_path = Path(name) / "summary.json"
        report_path.write_text(
            json.dumps(report, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n",
            encoding="utf-8",
            newline="\n",
        )
        ingest = market_history.ingest_multi_summary(
            report_path, config.database, observed_at=_iso(observed_at)
        )
    snapshot = export_economy_snapshot.build_snapshot(
        config.database,
        build=config.build,
        recipes_path=config.recipes,
        market_rules_path=config.rules,
        market_dimensions_path=config.dimensions,
        item_icons_path=config.icons,
        restrict_to_market_dimensions=True,
    )
    views = build_economy_views.build_views(snapshot)
    store = FileSystemEconomyStore(config.snapshots)
    previous = store.current_revision()
    digests: dict[str, str] = {}
    for name, value in sorted(views.items()):
        encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        digest = hashlib.sha256(encoded).hexdigest()
        store.stage_view(snapshot["revision"], name, encoded, digest)
        digests[name] = digest
    committed = store.commit_views(
        snapshot["revision"], previous, digests, uuid.uuid4().hex
    )
    release = hashlib.sha1(Path(__file__).read_bytes()).hexdigest()
    now = _now()
    run_id = "market-" + uuid.uuid4().hex
    server_count = len(report["responses"])
    rows = sum(int(response["summary_count"]) for response in report["responses"])
    status = {
        "schema_version": "aion2.market-autorefresh.status/v1",
        "observed_at_utc": _iso(now),
        "scan_observed_at_utc": _iso(observed_at),
        "state": "published",
        "trigger": "scheduled",
        "run_id": run_id,
        "revision": snapshot["revision"],
        "release": release,
        "server_count": server_count,
        "request_count": server_count,
        "transport_attempt_count": int(report["transport_attempts"]),
        "summary_rows": rows,
        "snapshot_changed": previous != snapshot["revision"],
        "packet_bus_segments": 1,
        "next_run_utc": _iso(now + timedelta(seconds=config.interval)),
        "last_success": {
            "observed_at_utc": _iso(observed_at),
            "run_id": run_id,
            "revision": snapshot["revision"],
            "release": release,
            "server_count": server_count,
            "request_count": server_count,
            "transport_attempt_count": int(report["transport_attempts"]),
            "summary_rows": rows,
            "snapshot_changed": previous != snapshot["revision"],
        },
    }
    encoded_status = json.dumps(status, sort_keys=True, separators=(",", ":")).encode("utf-8")
    store.write_crawler_status(
        snapshot["revision"], encoded_status, hashlib.sha256(encoded_status).hexdigest(), now=now
    )
    return {
        "status": "published",
        "revision": snapshot["revision"],
        "market_scopes": server_count,
        "summary_rows": rows,
        "active_items": ingest["active_items"],
        "views": committed["views"],
        "credentials_exported": False,
        "raw_exposed": False,
    }


def _last_scan(status_path: Path) -> datetime | None:
    try:
        value = json.loads(status_path.read_text(encoding="utf-8"))
        return datetime.fromisoformat(str(value["scan_observed_at_utc"]).replace("Z", "+00:00"))
    except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError):
        return None


def run() -> int:
    config = Config()
    for directory in (config.inbox, config.state, config.snapshots):
        directory.mkdir(parents=True, exist_ok=True)
    _initialize_database(config)
    template: dict[str, Any] | None = None
    template_at: datetime | None = None
    while True:
        bundles = sorted(config.inbox.glob("*.a2tlsbundle"), key=lambda path: path.stat().st_mtime_ns)
        if not bundles:
            time.sleep(config.poll_seconds)
            continue
        bundle = bundles[0]
        processing = bundle.with_suffix(bundle.suffix + ".processing")
        try:
            os.replace(bundle, processing)
            try:
                candidate = market_summary_probe._extract_template(processing)
                if template is not None:
                    template.get("headers", {}).clear()
                template = candidate
                template_at = _now()
            except ValueError as error:
                if str(error) not in {
                    "market_session_template_not_found",
                    "market_session_auth_missing",
                }:
                    raise
            due_at = _last_scan(config.status)
            scan_due = due_at is None or (_now() - due_at).total_seconds() >= config.interval
            template_fresh = (
                template is not None
                and template_at is not None
                and (_now() - template_at).total_seconds() <= config.session_max_age
            )
            if scan_due and template_fresh:
                server_ids, batch_sha256, registry_sha256 = market_summary_probe.load_target_batch(
                    config.target_batch
                )
                report = market_summary_probe.execute_multi_probe(
                    template,
                    server_ids,
                    batch_sha256,
                    registry_sha256,
                    sender=_direct_post,
                )
                template = None
                template_at = None
                receipt = _publish(config, report, _now())
                _write_json_atomic(config.status, {**receipt, "scan_observed_at_utc": _iso(_now())})
                print(json.dumps(receipt, sort_keys=True), flush=True)
            else:
                print(
                    json.dumps(
                        {
                            "status": "session_refreshed" if template_fresh else "waiting_for_market_session",
                            "scan_due": scan_due,
                            "credentials_exported": False,
                            "raw_exposed": False,
                        },
                        sort_keys=True,
                    ),
                    flush=True,
                )
        except KeyboardInterrupt:
            return 0
        except Exception as error:
            code = str(error)
            if code not in market_summary_probe.SAFE_ERRORS:
                code = "worker_refresh_failed"
            print(json.dumps({"status": "worker_error", "error": code, "raw_exposed": False}), flush=True)
        finally:
            processing.unlink(missing_ok=True)


if __name__ == "__main__":
    raise SystemExit(run())
