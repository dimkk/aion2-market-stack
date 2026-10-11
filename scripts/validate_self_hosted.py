#!/usr/bin/env python3
"""Validate the public self-hosted source package without requiring Docker."""

from __future__ import annotations

import argparse
import importlib.util
import json
from pathlib import Path
import re

ROOT = Path(__file__).resolve().parents[1]
DEPLOY = ROOT / "deploy" / "self-hosted"
REQUIRED = (
    ".dockerignore",
    "deploy/self-hosted/.env.example",
    "deploy/self-hosted/compose.yaml",
    "deploy/self-hosted/Dockerfile.api",
    "deploy/self-hosted/Dockerfile.worker",
    "deploy/self-hosted/scripts/Initialize-Env.ps1",
    "deploy/self-hosted/scripts/Prepare-Assets.ps1",
    "deploy/self-hosted/scripts/Prepare-SeedData.ps1",
    "deploy/self-hosted/scripts/Start-Stack.ps1",
    "deploy/self-hosted/scripts/Start-Collector.ps1",
    "deploy/self-hosted/scripts/Start-FarmTracker.ps1",
    "tools/public_collector/collector.py",
    "tools/farm_tracker/app.py",
    "tools/farm_tracker/contracts.py",
    "tools/farm_tracker/decoder.py",
    "tools/farm_tracker/history.py",
    "tools/farm_tracker/packet_bus.py",
    "tools/farm_tracker/quotes.py",
    "tools/farm_tracker/session.py",
    "tools/farm_tracker/sync.py",
    "tools/farm_tracker/data/zone-catalog.json",
    "services/market-worker/worker.py",
    "scripts/build_seed_from_database.py",
    "exports/economy/item-catalog.json",
    "exports/economy/item-icons.json",
    "exports/economy/recipes.json",
    "data/seed/manifest.json",
    "data/seed/state/market-history.sqlite",
    "data/seed/snapshots/current.json",
)
SECRET_KEYS = ("POSTGRES_PASSWORD", "SESSION_HMAC_KEY_B64")


def validate(require_assets: bool) -> dict[str, object]:
    missing = [name for name in REQUIRED if not (ROOT / name).is_file()]
    if missing:
        raise ValueError("self_hosted_required_files_missing:" + ",".join(missing))

    compose_text = (DEPLOY / "compose.yaml").read_text(encoding="utf-8")
    try:
        services_block = compose_text.split("\nservices:\n", 1)[1].split(
            "\nvolumes:\n", 1
        )[0]
    except IndexError as error:
        raise ValueError("self_hosted_compose_shape_invalid") from error
    services = {
        match.group(1)
        for match in re.finditer(r"(?m)^  ([a-z0-9-]+):\s*$", services_block)
    }
    if services != {
        "postgres",
        "api",
        "market-worker",
    }:
        raise ValueError("self_hosted_compose_services_invalid")
    if "- 127.0.0.1:${SITE_PORT:-8080}:8080" not in services_block:
        raise ValueError("self_hosted_api_not_loopback_bound")
    if re.search(r"(?m)^\s+privileged:\s*true\s*$", services_block) or "NET_ADMIN" in services_block:
        raise ValueError("self_hosted_container_privilege_invalid")

    env_text = (DEPLOY / ".env.example").read_text(encoding="utf-8")
    for key in SECRET_KEYS:
        match = re.search(rf"(?m)^{re.escape(key)}=(.+)$", env_text)
        if match is None or "replace-with-" not in match.group(1):
            raise ValueError("self_hosted_env_secret_example_invalid")
    if (
        "AION2_FARM_PACKET_BUS_ENABLED=false" not in env_text
        or not re.search(r"(?m)^AION2_FARM_MARKET_SERVER_ID=$", env_text)
    ):
        raise ValueError("self_hosted_farm_defaults_not_fail_closed")
    forbidden = ("authorization:", "bearer ", "jwt ey", "client_random ")
    if any(value in env_text.casefold() for value in forbidden):
        raise ValueError("self_hosted_env_contains_session_material")

    seed = json.loads((ROOT / "data/seed/manifest.json").read_text(encoding="utf-8"))
    if (
        seed.get("schemaVersion") != "aion2.public-seed/v2"
        or seed.get("marketScopes", 0) < 100
        or seed.get("summaryRuns", 0) < 1_000
        or seed.get("summaryRows", 0) < 300_000
        or seed.get("listingRows", 0) < 20_000
        or seed.get("rawExposed") is not False
        or seed.get("credentialsExported") is not False
    ):
        raise ValueError("self_hosted_seed_manifest_invalid")

    runtime_sources = "\n".join(
        (ROOT / name).read_text(encoding="utf-8")
        for name in (
            "deploy/self-hosted/Dockerfile.api",
            "deploy/self-hosted/Dockerfile.worker",
            "services/market-worker/worker.py",
        )
    ).casefold()
    if "vendor/" in runtime_sources:
        raise ValueError("self_hosted_runtime_has_vendored_dependency")
    for dockerfile_name in ("Dockerfile.api", "Dockerfile.worker"):
        dockerfile = (DEPLOY / dockerfile_name).read_text(encoding="utf-8")
        for line in dockerfile.splitlines():
            stripped = line.strip()
            if not stripped.startswith("COPY ") or stripped.startswith("COPY --from="):
                continue
            parts = stripped.removeprefix("COPY ").split()
            if len(parts) < 2 or any(not (ROOT / source).exists() for source in parts[:-1]):
                raise ValueError("self_hosted_docker_copy_source_missing")

    manifest = json.loads(
        (ROOT / "exports/economy/item-icons.json").read_text(encoding="utf-8")
    )
    items = manifest.get("items") if isinstance(manifest, dict) else None
    if manifest.get("schemaVersion") != "aion2.item-icons/v1" or not isinstance(items, list):
        raise ValueError("self_hosted_item_icon_manifest_invalid")
    assets_present = False
    if require_assets:
        stage_path = ROOT / "scripts" / "stage_economy_web_assets.py"
        specification = importlib.util.spec_from_file_location(
            "self_hosted_stage_assets", stage_path
        )
        if specification is None or specification.loader is None:
            raise ValueError("self_hosted_asset_validator_missing")
        module = importlib.util.module_from_spec(specification)
        specification.loader.exec_module(module)

        source = ROOT / "exports" / "economy" / "item-icons"
        destination = DEPLOY / "runtime" / "validation-public"
        module.stage_assets(
            ROOT / "exports/economy/item-icons.json", source, destination
        )
        assets_present = True

    return {
        "status": "self_hosted_source_valid",
        "services": sorted(services),
        "item_icon_manifest_rows": len(items),
        "client_derived_assets_validated": assets_present,
        "docker_required_for_runtime": True,
        "raw_exposed": False,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--require-assets", action="store_true")
    args = parser.parse_args()
    print(json.dumps(validate(args.require_assets), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
