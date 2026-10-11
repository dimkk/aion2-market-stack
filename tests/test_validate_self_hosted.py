from __future__ import annotations

import importlib.util
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "validate_self_hosted", ROOT / "scripts" / "validate_self_hosted.py"
)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def test_public_self_hosted_source_contract() -> None:
    receipt = MODULE.validate(require_assets=False)
    assert receipt["status"] == "self_hosted_source_valid"
    assert receipt["services"] == ["api", "market-worker", "postgres"]
    assert receipt["raw_exposed"] is False
