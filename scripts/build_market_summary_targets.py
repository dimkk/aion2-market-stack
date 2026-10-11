#!/usr/bin/env python3
"""Build a digest-bound multi-server summary target list from the registry."""

from __future__ import annotations

import argparse
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path


TRADE_SERVER_ID_OFFSET = 10_000


def trade_server_id(game_server_id: int) -> int:
    """Map the public lobby/style server id to the verified trade scope id.

    Live read-only controls on 2026-10-07 proved the same relation for EU
    Elyos, EU Asmodians and Asia Elyos: raw ids returned only the three
    Exchange/Kina products, while ``raw + 10000`` returned 527/557/541 normal
    auction items.  The full generated set is accepted only after every scope
    answers the normal-market summary validator.
    """
    if type(game_server_id) is not int or not 1 <= game_server_id <= 0xFFFFFFFF - TRADE_SERVER_ID_OFFSET:
        raise ValueError("invalid_global_server_registry")
    return game_server_id + TRADE_SERVER_ID_OFFSET


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--registry", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    raw = args.registry.read_bytes()
    registry = json.loads(raw.decode("utf-8"))
    servers = registry.get("servers", [])
    source_ids = [row.get("gameServerId") for row in servers if isinstance(row, dict)]
    if (
        registry.get("schemaVersion") != "aion2.global-server-registry/v1"
        or registry.get("serverCount") != len(source_ids)
        or not 1 <= len(source_ids) <= 100
        or len(set(source_ids)) != len(source_ids)
        or any(type(value) is not int or value <= 0 for value in source_ids)
    ):
        raise SystemExit("invalid_global_server_registry")
    try:
        ids = [trade_server_id(value) for value in source_ids]
    except ValueError as error:
        raise SystemExit(str(error)) from error
    payload = {
        "format": "aion2_market_summary_target_batch_v1",
        "generated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "registry_sha256": hashlib.sha256(raw).hexdigest(),
        "game_server_ids": ids,
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(payload, separators=(",", ":")) + "\n", encoding="utf-8")
    print(json.dumps({
        "status": "market_summary_targets_ready",
        "trade_server_ids": len(ids),
        "mapping": "game_server_id_plus_10000",
        "registry_sha256": payload["registry_sha256"],
    }, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
