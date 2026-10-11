#!/usr/bin/env python3
"""Build a sanitized starter SQLite database and immutable Economy views."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import shutil
import sqlite3
import sys
import uuid


ROOT = Path(__file__).resolve().parents[1]
for module_root in (ROOT / "scripts", ROOT / "services" / "economy-api"):
    sys.path.insert(0, str(module_root))

import build_economy_views  # noqa: E402
import export_economy_snapshot  # noqa: E402
import market_history  # noqa: E402
from economy_api.filesystem_store import FileSystemEconomyStore  # noqa: E402


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def build(summary: Path, output: Path, observed_at: str, build_id: int) -> dict[str, object]:
    document = json.loads(summary.read_text(encoding="utf-8"))
    report = document.get("report", document)
    privacy = report.get("privacy", {}) if isinstance(report, dict) else {}
    if (
        not isinstance(report, dict)
        or document.get("raw_exposed", False) is not False
        or privacy.get("credentials_exported") is not False
        or privacy.get("raw_headers_exported") is not False
        or privacy.get("raw_bodies_exported") is not False
    ):
        raise ValueError("seed_source_is_not_sanitized")

    readme = (output / "README.md").read_bytes() if (output / "README.md").is_file() else None
    staging = output.with_name(output.name + ".staging")
    if staging.exists():
        shutil.rmtree(staging)
    (staging / "state").mkdir(parents=True)
    snapshots = staging / "snapshots"
    database = staging / "state" / "market-history.sqlite"

    catalog = ROOT / "exports" / "economy" / "item-catalog.json"
    catalog_receipt = market_history.import_catalog(catalog, database, build_id, build_id)
    ingest_source = summary
    normalized_legacy_contract = False
    server_ids = report.get("request", {}).get("game_server_ids", [])
    if (
        "transport_attempts" not in report
        and isinstance(server_ids, list)
        and server_ids
        and privacy.get("max_requests") == len(server_ids)
        and all(row.get("status_code") == 200 for row in report.get("responses", []))
    ):
        # The first bounded multi-summary exporter recorded one successful
        # attempt per scope but predated the explicit transport_attempts field.
        # Adapt only this fully successful sanitized legacy shape for the
        # current strict offline ingester; the original source hash is retained.
        normalized = json.loads(json.dumps(document))
        normalized_report = normalized.get("report", normalized)
        normalized_report["transport_attempts"] = len(server_ids)
        normalized_report["privacy"]["max_requests"] = (
            len(server_ids) * market_history.MAX_SUMMARY_TRANSPORT_ATTEMPTS_PER_SCOPE
        )
        ingest_source = staging / "normalized-summary.json"
        ingest_source.write_text(
            json.dumps(normalized, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
            encoding="utf-8",
            newline="\n",
        )
        normalized_legacy_contract = True
    ingest = market_history.ingest_multi_summary(
        ingest_source, database, observed_at=observed_at
    )
    if ingest_source != summary:
        ingest_source.unlink()
    snapshot = export_economy_snapshot.build_snapshot(
        database,
        build=build_id,
        recipes_path=ROOT / "exports" / "economy" / "recipes.json",
        market_rules_path=ROOT / "configs" / "global-market-rules.json",
        market_dimensions_path=ROOT / "configs" / "global-market-dimensions.json",
        item_icons_path=ROOT / "exports" / "economy" / "item-icons.json",
        restrict_to_market_dimensions=True,
    )
    views = build_economy_views.build_views(snapshot)
    store = FileSystemEconomyStore(snapshots)
    digests: dict[str, str] = {}
    for name, value in sorted(views.items()):
        encoded = json.dumps(
            value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        digest = hashlib.sha256(encoded).hexdigest()
        store.stage_view(snapshot["revision"], name, encoded, digest)
        digests[name] = digest
    committed = store.commit_views(
        snapshot["revision"], None, digests, uuid.uuid4().hex
    )

    connection = sqlite3.connect(database)
    try:
        connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        connection.execute("VACUUM")
    finally:
        connection.close()

    manifest = {
        "schemaVersion": "aion2.public-seed/v1",
        "observedAt": observed_at,
        "build": build_id,
        "sourceSha256": _sha256(summary),
        "databaseSha256": _sha256(database),
        "revision": snapshot["revision"],
        "marketScopes": ingest["servers"],
        "summaryRows": ingest["summaries"],
        "activeItems": ingest["active_items"],
        "catalogItems": catalog_receipt["items"],
        "views": committed["views"],
        "rawExposed": False,
        "credentialsExported": False,
        "legacyContractNormalized": normalized_legacy_contract,
        "limitations": [
            "Historical sample; not current market data.",
            "Summary rows are not individual auction listings.",
            "Game-derived data is not covered by the source-code license.",
        ],
    }
    (staging / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    if readme is not None:
        (staging / "README.md").write_bytes(readme)

    if output.exists():
        shutil.rmtree(output)
    shutil.move(str(staging), str(output))
    return manifest


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--summary", required=True, type=Path)
    parser.add_argument("--output", type=Path, default=ROOT / "data" / "seed")
    parser.add_argument("--observed-at", required=True)
    parser.add_argument("--build", type=int, default=25650019)
    args = parser.parse_args()
    receipt = build(args.summary.resolve(), args.output.resolve(), args.observed_at, args.build)
    print(json.dumps(receipt, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
