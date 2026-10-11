from __future__ import annotations

import argparse
import hashlib
import json
import re
from pathlib import Path
from typing import Any

_NAME = re.compile(r"^(market|items|movers|servers|craft|farm-value)\.([a-z0-9_-]{1,48})\.json$")
_REVISION = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")
_MAX_BYTES = 8 * 1024 * 1024


def _load(path: Path) -> tuple[dict[str, Any], str]:
    encoded = path.read_bytes()
    if len(encoded) > _MAX_BYTES:
        raise ValueError(f"view_too_large:{path.name}")
    value = json.loads(encoded)
    if not isinstance(value, dict):
        raise ValueError(f"view_must_be_object:{path.name}")
    revision = value.get("revision")
    if not isinstance(revision, str) or not _REVISION.fullmatch(revision):
        raise ValueError(f"view_revision_required:{path.name}")
    return value, hashlib.sha256(encoded).hexdigest()


def _write_verified(path: Path, encoded: bytes, digest: str, *, immutable: bool) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and immutable:
        current = hashlib.sha256(path.read_bytes()).hexdigest()
        if current != digest:
            raise ValueError(f"immutable_revision_conflict:{path}")
        return
    path.write_bytes(encoded)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Prepare sanitized economy snapshot objects for an owner-run yc upload."
    )
    parser.add_argument("--directory", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args(argv)
    prepared: list[tuple[Path, str, str, dict[str, Any], str]] = []
    for path in sorted(args.directory.glob("*.json")):
        matched = _NAME.fullmatch(path.name)
        if not matched:
            continue
        value, digest = _load(path)
        view, segment = matched.groups()
        prepared.append((path, view, segment, value, digest))
    if not prepared:
        raise SystemExit("no_view_files_found")
    revisions = {str(item[3]["revision"]) for item in prepared}
    if len(revisions) != 1:
        raise SystemExit("mixed_snapshot_revisions")

    for path, view, segment, value, digest in prepared:
        encoded = path.read_bytes()
        revision = value["revision"]
        version_path = (
            args.output / "snapshots" / "versions" / revision / view / f"{segment}.json"
        )
        current_path = args.output / "snapshots" / "current" / view / f"{segment}.json"
        _write_verified(version_path, encoded, digest, immutable=True)
        _write_verified(current_path, encoded, digest, immutable=False)
        print(
            f"snapshot_prepared view={view} segment={segment} "
            f"revision={revision} sha256={digest}"
        )
    print(f"prepare_complete views={len(prepared)} revision={next(iter(revisions))}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
