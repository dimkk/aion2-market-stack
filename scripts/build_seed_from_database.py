#!/usr/bin/env python3
"""Publish a checked, credential-free market-history database as the starter dataset."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re
import shutil
import sqlite3
import sys
import uuid


ROOT = Path(__file__).resolve().parents[1]
for module_root in (ROOT / "scripts", ROOT / "services" / "economy-api"):
    sys.path.insert(0, str(module_root))

import build_economy_views  # noqa: E402
import export_economy_snapshot  # noqa: E402
from economy_api.filesystem_store import FileSystemEconomyStore  # noqa: E402


EXPECTED_TABLES = {
    "catalogs",
    "item_server_state",
    "items",
    "listings",
    "scan_items",
    "scans",
    "summary_prices",
    "summary_runs",
}
FORBIDDEN_COLUMN_PARTS = {
    "account",
    "authorization",
    "body",
    "character",
    "cookie",
    "credential",
    "header",
    "jwt",
    "keylog",
    "password",
    "payload",
    "seller",
    "secret",
    "session",
}
FORBIDDEN_VALUE_PATTERNS = (
    re.compile(r"-----BEGIN (?:RSA |OPENSSH |EC )?PRIVATE KEY-----"),
    re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b"),
    re.compile(r"(?i)\b(?:authorization|cookie)\s*[:=]"),
    re.compile(r"(?i)\bclient_random\s+[A-F0-9]{16,}"),
    re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._-]{16,}"),
    re.compile(r"(?i)\b[A-Z]:\\"),
    re.compile(r"(?i)https?://"),
)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _scalar(connection: sqlite3.Connection, query: str) -> int:
    return int(connection.execute(query).fetchone()[0])


def _audit_database(connection: sqlite3.Connection) -> None:
    tables = {
        str(row[0])
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )
    }
    if tables != EXPECTED_TABLES:
        raise ValueError("seed_database_tables_invalid")

    for table in sorted(tables):
        columns = list(connection.execute(f'PRAGMA table_info("{table}")'))
        for column in columns:
            name = str(column[1]).casefold()
            if any(part in name for part in FORBIDDEN_COLUMN_PARTS):
                raise ValueError(f"seed_database_sensitive_column:{table}.{name}")
            if str(column[2]).upper() != "TEXT":
                continue
            for (value,) in connection.execute(
                f'SELECT "{column[1]}" FROM "{table}" WHERE "{column[1]}" IS NOT NULL'
            ):
                if isinstance(value, str) and any(
                    pattern.search(value) for pattern in FORBIDDEN_VALUE_PATTERNS
                ):
                    raise ValueError(f"seed_database_sensitive_value:{table}.{name}")


def build(database_source: Path, output: Path, build_id: int) -> dict[str, object]:
    readme = (output / "README.md").read_bytes() if (output / "README.md").is_file() else None
    staging = output.with_name(output.name + ".staging")
    if staging.exists():
        shutil.rmtree(staging)
    (staging / "state").mkdir(parents=True)
    snapshots = staging / "snapshots"
    database = staging / "state" / "market-history.sqlite"

    source = sqlite3.connect(
        f"file:{database_source.resolve().as_posix()}?mode=ro", uri=True
    )
    target = sqlite3.connect(database)
    try:
        _audit_database(source)
        source.backup(target)
        target.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        target.execute("PRAGMA journal_mode=DELETE")
        target.execute("VACUUM")
    finally:
        target.close()
        source.close()

    connection = sqlite3.connect(database)
    try:
        _audit_database(connection)
        summary_runs = _scalar(connection, "SELECT count(*) FROM summary_runs")
        summary_rows = _scalar(connection, "SELECT count(*) FROM summary_prices")
        listing_rows = _scalar(connection, "SELECT count(*) FROM listings")
        market_scopes = _scalar(
            connection, "SELECT count(DISTINCT game_server_id) FROM summary_runs"
        )
        listing_scopes = _scalar(
            connection, "SELECT count(DISTINCT game_server_id) FROM scans"
        )
        catalog_items = _scalar(connection, "SELECT count(*) FROM items")
        observed_from, observed_to = connection.execute(
            "SELECT min(observed_at),max(observed_at) FROM summary_runs"
        ).fetchone()
    finally:
        connection.close()

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
    committed = store.commit_views(snapshot["revision"], None, digests, uuid.uuid4().hex)

    manifest = {
        "schemaVersion": "aion2.public-seed/v2",
        "build": build_id,
        "databaseSha256": _sha256(database),
        "revision": snapshot["revision"],
        "observedFrom": observed_from,
        "observedTo": observed_to,
        "marketScopes": market_scopes,
        "listingScopes": listing_scopes,
        "summaryRuns": summary_runs,
        "summaryRows": summary_rows,
        "listingRows": listing_rows,
        "catalogItems": catalog_items,
        "views": committed["views"],
        "rawExposed": False,
        "credentialsExported": False,
        "limitations": [
            "Historical derived market observations; not live prices.",
            "The database contains every safe derived row available at publication time, not every listing that ever existed.",
            "Raw captures, transport/session material, account data, and character data are excluded.",
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
    parser.add_argument("--database", required=True, type=Path)
    parser.add_argument("--output", type=Path, default=ROOT / "data" / "seed")
    parser.add_argument("--build", type=int, default=25650019)
    args = parser.parse_args()
    receipt = build(args.database.resolve(), args.output.resolve(), args.build)
    print(json.dumps(receipt, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
