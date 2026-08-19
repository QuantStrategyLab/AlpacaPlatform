"""Create one bounded P5 shadow-cycle input from a UESP forward observation.

This adapter has no broker client, scheduler, credential, market-data, or
network dependency.  It only joins already-produced local artifacts and writes
the result to a create-only path.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from .shadow_ledger import ShadowLedgerError, build_tqqq_shadow_cycle_input


def _no_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ShadowLedgerError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _load_json(path: Path, label: str) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=_no_duplicate_keys)
    except (OSError, json.JSONDecodeError) as exc:
        raise ShadowLedgerError(f"invalid {label} JSON") from exc


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build one pure, no-broker TQQQ P5 shadow-cycle input")
    parser.add_argument("--forward-observation", type=Path, required=True)
    parser.add_argument("--policy-gate-receipt", type=Path, required=True)
    parser.add_argument("--risk-control", type=Path, required=True)
    parser.add_argument("--deployment-bundle-sha256", required=True)
    parser.add_argument("--cycle-id", required=True)
    parser.add_argument("--produced-at", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        cycle = build_tqqq_shadow_cycle_input(
            forward_observation=_load_json(args.forward_observation, "forward observation"),
            policy_gate_receipt=_load_json(args.policy_gate_receipt, "policy-gate receipt"),
            risk_control=_load_json(args.risk_control, "risk control"),
            deployment_bundle_sha256=args.deployment_bundle_sha256,
            cycle_id=args.cycle_id,
            produced_at=args.produced_at,
        )
        with args.output.open("x", encoding="utf-8") as handle:
            handle.write(json.dumps(cycle, sort_keys=True, separators=(",", ":"), ensure_ascii=True))
            handle.write("\n")
    except (OSError, ShadowLedgerError) as exc:
        print(f"shadow-cycle input failed: {exc}", file=sys.stderr)
        return 1
    print(f"SHADOW_CYCLE_INPUT_RECORDED cycle={cycle['cycle_id']} input_sha256={cycle['input_sha256']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
