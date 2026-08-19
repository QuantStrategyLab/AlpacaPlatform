from __future__ import annotations

import copy
import json
import subprocess
import sys
from pathlib import Path

import pytest

from alpaca_platform import shadow_ledger


def sha(character: str) -> str:
    return character * 64


def revision(character: str) -> str:
    return character * 40


def cycle_input(*, session: str = "2026-08-20", allocation: dict[str, int] | None = None) -> dict[str, object]:
    allocation = allocation or {"TQQQ": 4_500, "QQQM": 4_500, "BOXX": 800, "CASH": 200}
    decision: dict[str, object] = {
        "decision_id": f"tqqq_core_only_p2_v5_{session.replace('-', '')}",
        "effective_session": session,
        "producer_revision": revision("a"),
        "allocation_bps": allocation,
        "decision_sha256": "",
    }
    decision["decision_sha256"] = shadow_ledger.calculate_decision_sha256(decision)
    payload: dict[str, object] = {
        "schema": shadow_ledger.INPUT_SCHEMA,
        "cycle_id": f"tqqq_core_only_p2_v5_shadow_{session.replace('-', '')}",
        "produced_at": "2026-08-20T20:00:00Z",
        "candidate": {
            "candidate_id": shadow_ledger.CANDIDATE_ID,
            "config_sha256": sha("b"),
            "strategy_repository": "QuantStrategyLab/UsEquityStrategies",
            "strategy_revision": revision("a"),
        },
        "source_evidence": {
            "p1_manifest_sha256": sha("c"),
            "p2_config_sha256": sha("b"),
            "p3_evidence_sha256": sha("d"),
            "producer_revision": revision("e"),
        },
        "forward_decision": decision,
        "risk_control": {
            "stage": "SHADOW",
            "execution_lane": "SHADOW_LEDGER",
            "risk_policy_id": "tqqq-core-only-shadow",
            "risk_policy_version": "v1",
            "risk_policy_sha256": sha("f"),
        },
        "input_sha256": "",
    }
    payload["input_sha256"] = shadow_ledger.calculate_input_sha256(payload)
    return payload


def test_genesis_receipt_is_chain_linked_and_contains_only_virtual_weight_changes():
    receipt = shadow_ledger.build_shadow_ledger_receipt(cycle_input())

    assert receipt["schema"] == shadow_ledger.RECEIPT_SCHEMA
    assert receipt["ledger_parent_sha256"] == "0" * 64
    assert receipt["shadow_adjustments_bps"] == {"TQQQ": 4_500, "QQQM": 4_500, "BOXX": 800, "CASH": -9_800}
    assert shadow_ledger.validate_shadow_ledger_receipt(receipt) == receipt
    rendered = json.dumps(receipt, sort_keys=True)
    for forbidden in ("broker", "order", "account", "notional", "price", "credential"):
        assert forbidden not in rendered.lower()


def test_next_session_links_to_previous_virtual_allocation_without_broker_state():
    first = shadow_ledger.build_shadow_ledger_receipt(cycle_input())
    second = shadow_ledger.build_shadow_ledger_receipt(
        cycle_input(
            session="2026-08-21",
            allocation={"TQQQ": 0, "QQQM": 9_000, "BOXX": 800, "CASH": 200},
        ),
        prior_receipt=first,
    )

    assert second["ledger_parent_sha256"] == first["receipt_sha256"]
    assert second["shadow_adjustments_bps"] == {"TQQQ": -4_500, "QQQM": 4_500, "BOXX": 0, "CASH": 0}


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        (("risk_control", "execution_lane", "PAPER_BROKER"), "SHADOW / SHADOW_LEDGER"),
        (("source_evidence", "p2_config_sha256", sha("z")), "must match candidate"),
        (
            ("forward_decision", "allocation_bps", {"TQQQ": 4_500, "QQQM": 4_500, "BOXX": 800, "CASH": 100}),
            "sum to 10000",
        ),
        (("broker_endpoint", None, "https://paper-api.example"), "forbidden"),
    ],
)
def test_mutations_and_sensitive_material_fail_closed(mutation: tuple[str, str | None, object], message: str):
    payload = cycle_input()
    parent, child, value = mutation
    if child is None:
        payload[parent] = value
    else:
        payload[parent][child] = value
    if parent != "broker_endpoint":
        if parent == "forward_decision":
            payload["forward_decision"]["decision_sha256"] = shadow_ledger.calculate_decision_sha256(
                payload["forward_decision"]
            )
        payload["input_sha256"] = shadow_ledger.calculate_input_sha256(payload)

    with pytest.raises(shadow_ledger.ShadowLedgerError, match=message):
        shadow_ledger.build_shadow_ledger_receipt(payload)


def test_prior_receipt_requires_same_candidate_and_a_later_session():
    prior = shadow_ledger.build_shadow_ledger_receipt(cycle_input())

    with pytest.raises(shadow_ledger.ShadowLedgerError, match="must advance"):
        shadow_ledger.build_shadow_ledger_receipt(cycle_input(), prior_receipt=prior)

    mismatched_candidate = copy.deepcopy(prior)
    mismatched_candidate["candidate"]["config_sha256"] = sha("0")
    mismatched_candidate["receipt_sha256"] = shadow_ledger.calculate_receipt_sha256(mismatched_candidate)
    with pytest.raises(shadow_ledger.ShadowLedgerError, match="candidate"):
        shadow_ledger.build_shadow_ledger_receipt(cycle_input(session="2026-08-21"), prior_receipt=mismatched_candidate)


def test_cli_creates_one_receipt_and_refuses_to_overwrite(tmp_path: Path):
    input_path = tmp_path / "input.json"
    output_path = tmp_path / "receipt.json"
    input_path.write_text(json.dumps(cycle_input()), encoding="utf-8")
    command = [
        sys.executable,
        "-m",
        "alpaca_platform.shadow_ledger",
        "--input",
        str(input_path),
        "--output",
        str(output_path),
    ]
    first = subprocess.run(command, capture_output=True, check=False, text=True)
    second = subprocess.run(command, capture_output=True, check=False, text=True)

    assert first.returncode == 0, first.stderr
    assert first.stdout.startswith("SHADOW_RECEIPT_RECORDED cycle=tqqq_core_only_p2_v5_shadow_20260820")
    assert shadow_ledger.validate_shadow_ledger_receipt(json.loads(output_path.read_text(encoding="utf-8")))
    assert second.returncode == 1
    assert "File exists" in second.stderr
