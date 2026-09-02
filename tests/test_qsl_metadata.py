from __future__ import annotations

import tomllib
from pathlib import Path


def test_qsl_metadata_registers_alpaca_runtime() -> None:
    qsl_path = Path(__file__).resolve().parents[1] / "qsl.toml"
    with qsl_path.open("rb") as f:
        qsl = tomllib.load(f)["qsl"]

    assert qsl["repo"] == "AlpacaPlatform"
    assert qsl["tier"] == "runtime"
    assert qsl["upgrade_ring"] == "ring_d"
    assert qsl["allow_legacy"] is False
    assert qsl["enforce_bundle"] is True
    assert qsl["owner"] == "runtime-platform"
    assert qsl["compat"]["bundle"] == "2026.09.0"
    assert "requires" not in qsl
