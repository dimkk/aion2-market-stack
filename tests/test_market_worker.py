from __future__ import annotations

import importlib.util
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _module(monkeypatch):
    monkeypatch.setenv("AION2_APP_ROOT", str(ROOT))
    specification = importlib.util.spec_from_file_location(
        "self_hosted_market_worker", ROOT / "services/market-worker/worker.py"
    )
    assert specification and specification.loader
    module = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(module)
    return module


def test_sanitized_summary_reaches_sqlite_and_immutable_views(
    tmp_path: Path, monkeypatch
) -> None:
    worker = _module(monkeypatch)
    config = worker.Config()
    config.state = tmp_path / "state"
    config.snapshots = tmp_path / "snapshots"
    config.database = config.state / "market-history.sqlite"
    worker._initialize_database(config)

    server_ids, target_digest, registry_digest = (
        worker.market_summary_probe.load_target_batch(config.target_batch)
    )
    responses = [
        {
            "game_server_id": server_id,
            "status_code": 200,
            "summary_count": 1,
            "summaries": [
                {
                    "game_item_id": 516150001,
                    "min_unit_price": "400000.0000",
                    "sale_count": 3,
                    "conditions": [],
                }
            ],
        }
        for server_id in server_ids
    ]
    report = {
        "format": worker.market_summary_probe.MULTI_FORMAT,
        "authority": worker.market_summary_probe.AUTHORITY,
        "method": worker.market_summary_probe.METHOD,
        "path": worker.market_summary_probe.PATH,
        "target_batch_sha256": target_digest,
        "registry_sha256": registry_digest,
        "request": {
            "game_server_ids": server_ids,
            "minimum_interval_seconds": 1.0,
        },
        "responses": responses,
        "transport_attempts": len(server_ids),
        "auth": {
            "kind": "client_session_jwt_memory_only",
            "client_must_remain_open": True,
            "persisted": False,
            "exported": False,
        },
        "privacy": {
            "fresh_https_request": True,
            "max_requests": len(server_ids)
            * worker.market_history.MAX_SUMMARY_TRANSPORT_ATTEMPTS_PER_SCOPE,
            "credentials_exported": False,
            "raw_headers_exported": False,
            "raw_bodies_exported": False,
            "paths_exposed": False,
        },
    }

    receipt = worker._publish(config, report, worker._now())

    assert receipt["status"] == "published"
    assert receipt["market_scopes"] == 100
    assert receipt["summary_rows"] == 100
    assert receipt["raw_exposed"] is False
    pointer = json.loads((config.snapshots / "current.json").read_text())
    assert pointer["revision"] == receipt["revision"]
    status_path = (
        config.snapshots
        / "versions"
        / receipt["revision"]
        / "crawler-status.json"
    )
    status = json.loads(status_path.read_text())
    assert status["revision"] == receipt["revision"]
    assert status["server_count"] == 100
