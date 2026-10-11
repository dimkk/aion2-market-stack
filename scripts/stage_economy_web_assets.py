#!/usr/bin/env python3
"""Stage validated content-hashed item PNGs into an Economy web build."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import struct
import tempfile
from pathlib import Path
from typing import Any


SCHEMA_VERSION = "aion2.item-icons/v1"
FILE_NAME = re.compile(r"^item-([0-9]+)-([0-9a-f]{16})\.png$")
SHA256 = re.compile(r"^[0-9a-f]{64}$")
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"


def _png_dimensions(data: bytes) -> tuple[int, int]:
    if len(data) < 24 or data[:8] != PNG_SIGNATURE or data[12:16] != b"IHDR":
        raise ValueError("item_icon_png_invalid")
    width, height = struct.unpack(">II", data[16:24])
    if width <= 0 or height <= 0:
        raise ValueError("item_icon_png_invalid")
    return width, height


def stage_assets(manifest_path: Path, source_dir: Path, dist_dir: Path) -> dict[str, Any]:
    manifest_bytes = manifest_path.read_bytes()
    manifest = json.loads(manifest_bytes)
    if (
        not isinstance(manifest, dict)
        or manifest.get("schemaVersion") != SCHEMA_VERSION
        or not isinstance(manifest.get("items"), list)
        or not manifest["items"]
    ):
        raise ValueError("item_icon_manifest_invalid")

    assets_dir = dist_dir / "assets"
    assets_dir.mkdir(parents=True, exist_ok=True)
    seen: set[str] = set()
    total_bytes = 0
    for row in manifest["items"]:
        if not isinstance(row, dict):
            raise ValueError("item_icon_manifest_row_invalid")
        file_name = row.get("fileName")
        sha256 = row.get("sha256")
        item_id = row.get("itemId")
        match = FILE_NAME.fullmatch(file_name) if isinstance(file_name, str) else None
        if (
            match is None
            or not isinstance(item_id, int)
            or item_id < 0
            or int(match.group(1)) != item_id
            or not isinstance(sha256, str)
            or SHA256.fullmatch(sha256) is None
            or match.group(2) != sha256[:16]
            or row.get("url") != f"/assets/{file_name}"
            or file_name in seen
        ):
            raise ValueError("item_icon_manifest_row_invalid")
        seen.add(file_name)

        source = source_dir / file_name
        data = source.read_bytes()
        if hashlib.sha256(data).hexdigest() != sha256:
            raise ValueError("item_icon_sha256_mismatch")
        width, height = _png_dimensions(data)
        if row.get("width") != width or row.get("height") != height:
            raise ValueError("item_icon_dimensions_mismatch")

        destination = assets_dir / file_name
        if destination.exists():
            if destination.read_bytes() != data:
                raise ValueError("item_icon_destination_conflict")
        else:
            handle, temporary_name = tempfile.mkstemp(
                prefix=f".{file_name}.", suffix=".tmp", dir=assets_dir
            )
            os.close(handle)
            temporary = Path(temporary_name)
            try:
                shutil.copyfile(source, temporary)
                os.replace(temporary, destination)
            finally:
                temporary.unlink(missing_ok=True)
        total_bytes += len(data)

    return {
        "status": "economy_web_assets_staged",
        "items": len(seen),
        "bytes": total_bytes,
        "manifestSha256": hashlib.sha256(manifest_bytes).hexdigest(),
        "rawExposed": False,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--source-dir", type=Path, required=True)
    parser.add_argument("--dist-dir", type=Path, required=True)
    args = parser.parse_args()
    receipt = stage_assets(args.manifest, args.source_dir, args.dist_dir)
    print(json.dumps(receipt, separators=(",", ":"), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
