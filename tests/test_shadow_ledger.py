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


def policy_gate_receipt() -> dict[str, object]:
    receipt: dict[str, object] = {
        "schema": shadow_ledger.POLICY_GATE_RECEIPT_SCHEMA,
        "verified_at": "2026-08-20T19:00:00Z",
        "deployment_bundle": {
            "schema": "qsl.deployment_bundle.v1",
            "bundle_id": "bundle.tqqq-core-only.shadow.20260820",
            "bundle_sha256": sha("1"),
        },
        "policy": {
            "policy_id": "tqqq-core-only-shadow-policy",
            "policy_version": "v1",
            "policy_sha256": sha("2"),
            "stage": "SHADOW",
            "effective_at": "2026-08-20T18:00:00Z",
            "expires_at": "2026-08-21T18:00:00Z",
        },
        "activation": {
            "activation_id": "tqqq-core-only-shadow-activation",
            "activation_sha256": sha("3"),
            "effective_at": "2026-08-20T19:00:00Z",
            "expires_at": "2026-08-21T17:00:00Z",
        },
        "target": {
            "platform": "alpaca",
            "repository": "QuantStrategyLab/AlpacaPlatform",
            "revision": revision("4"),
            "environment": "alpaca-shadow",
            "target_sha256": sha("5"),
        },
        "risk_control": {
            "risk_policy_id": "tqqq-core-only-shadow",
            "risk_policy_version": "v1",
            "risk_policy_sha256": sha("f"),
        },
        "trusted_policy_root": {
            "root_id": "qsl-kms-root-2026",
            "trusted_policy_root_sha256": sha("6"),
            "expires_at": "2026-08-22T00:00:00Z",
        },
        "signature_sha256": sha("7"),
        "receipt_sha256": "",
    }
    receipt["receipt_sha256"] = shadow_ledger.calculate_policy_gate_receipt_sha256(receipt)
    return receipt


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
        "deployment_bundle_sha256": sha("1"),
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
        "policy_gate_receipt": policy_gate_receipt(),
        "input_sha256": "",
    }
    payload["input_sha256"] = shadow_ledger.calculate_input_sha256(payload)
    return payload


def forward_observation(*, allocation: dict[str, int] | None = None) -> dict[str, object]:
    cycle = cycle_input(allocation=allocation)
    observation: dict[str, object] = {
        "schema": shadow_ledger.FORWARD_OBSERVATION_SCHEMA,
        "produced_at": "2026-08-20T19:30:00Z",
        "candidate": cycle["candidate"],
        "source_evidence": cycle["source_evidence"],
        "forward_decision": cycle["forward_decision"],
        "forward_observation_sha256": "",
    }
    observation["forward_observation_sha256"] = shadow_ledger.calculate_forward_observation_sha256(observation)
    return observation


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


def test_policy_gate_receipt_must_match_shadow_risk_control_and_cycle_window():
    mismatched_bundle = cycle_input()
    mismatched_bundle["deployment_bundle_sha256"] = sha("0")
    mismatched_bundle["input_sha256"] = shadow_ledger.calculate_input_sha256(mismatched_bundle)
    with pytest.raises(shadow_ledger.ShadowLedgerError, match="deployment bundle does not match"):
        shadow_ledger.build_shadow_ledger_receipt(mismatched_bundle)

    mismatched_risk = cycle_input()
    mismatched_risk["policy_gate_receipt"]["risk_control"]["risk_policy_sha256"] = sha("0")
    mismatched_risk["policy_gate_receipt"]["receipt_sha256"] = shadow_ledger.calculate_policy_gate_receipt_sha256(
        mismatched_risk["policy_gate_receipt"]
    )
    mismatched_risk["input_sha256"] = shadow_ledger.calculate_input_sha256(mismatched_risk)
    with pytest.raises(shadow_ledger.ShadowLedgerError, match="risk control does not match"):
        shadow_ledger.build_shadow_ledger_receipt(mismatched_risk)

    wrong_gateway = cycle_input()
    wrong_gateway["policy_gate_receipt"]["target"]["platform"] = "interactive-brokers"
    wrong_gateway["policy_gate_receipt"]["receipt_sha256"] = shadow_ledger.calculate_policy_gate_receipt_sha256(
        wrong_gateway["policy_gate_receipt"]
    )
    wrong_gateway["input_sha256"] = shadow_ledger.calculate_input_sha256(wrong_gateway)
    with pytest.raises(shadow_ledger.ShadowLedgerError, match="must bind this Alpaca"):
        shadow_ledger.build_shadow_ledger_receipt(wrong_gateway)

    expired = cycle_input()
    expired["policy_gate_receipt"]["activation"]["expires_at"] = "2026-08-20T20:00:00Z"
    expired["policy_gate_receipt"]["receipt_sha256"] = shadow_ledger.calculate_policy_gate_receipt_sha256(
        expired["policy_gate_receipt"]
    )
    expired["input_sha256"] = shadow_ledger.calculate_input_sha256(expired)
    with pytest.raises(shadow_ledger.ShadowLedgerError, match="expired"):
        shadow_ledger.build_shadow_ledger_receipt(expired)


def test_forward_observation_adapter_builds_the_only_accepted_p5_cycle_input():
    expected = cycle_input()
    built = shadow_ledger.build_tqqq_shadow_cycle_input(
        forward_observation=forward_observation(),
        policy_gate_receipt=expected["policy_gate_receipt"],
        risk_control=expected["risk_control"],
        deployment_bundle_sha256=expected["deployment_bundle_sha256"],
        cycle_id=expected["cycle_id"],
        produced_at=expected["produced_at"],
    )

    assert built == expected
    assert shadow_ledger.build_shadow_ledger_receipt(built)["forward_decision"] == expected["forward_decision"]

    corrupted = forward_observation()
    corrupted["forward_decision"]["allocation_bps"]["TQQQ"] = 0
    with pytest.raises(shadow_ledger.ShadowLedgerError, match="forward_observation|forward_decision"):
        shadow_ledger.build_tqqq_shadow_cycle_input(
            forward_observation=corrupted,
            policy_gate_receipt=expected["policy_gate_receipt"],
            risk_control=expected["risk_control"],
            deployment_bundle_sha256=expected["deployment_bundle_sha256"],
            cycle_id=expected["cycle_id"],
            produced_at=expected["produced_at"],
        )

    with pytest.raises(shadow_ledger.ShadowLedgerError, match="must not precede"):
        shadow_ledger.build_tqqq_shadow_cycle_input(
            forward_observation=forward_observation(),
            policy_gate_receipt=expected["policy_gate_receipt"],
            risk_control=expected["risk_control"],
            deployment_bundle_sha256=expected["deployment_bundle_sha256"],
            cycle_id=expected["cycle_id"],
            produced_at="2026-08-20T19:29:59Z",
        )


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


def test_cycle_input_cli_creates_one_valid_input_and_refuses_to_overwrite(tmp_path: Path):
    expected = cycle_input()
    forward_path = tmp_path / "forward-observation.json"
    policy_path = tmp_path / "policy-gate-receipt.json"
    risk_path = tmp_path / "risk-control.json"
    output_path = tmp_path / "cycle-input.json"
    forward_path.write_text(json.dumps(forward_observation()), encoding="utf-8")
    policy_path.write_text(json.dumps(expected["policy_gate_receipt"]), encoding="utf-8")
    risk_path.write_text(json.dumps(expected["risk_control"]), encoding="utf-8")
    command = [
        sys.executable,
        "-m",
        "alpaca_platform.shadow_cycle_input",
        "--forward-observation",
        str(forward_path),
        "--policy-gate-receipt",
        str(policy_path),
        "--risk-control",
        str(risk_path),
        "--deployment-bundle-sha256",
        expected["deployment_bundle_sha256"],
        "--cycle-id",
        expected["cycle_id"],
        "--produced-at",
        expected["produced_at"],
        "--output",
        str(output_path),
    ]
    first = subprocess.run(command, capture_output=True, check=False, text=True)
    second = subprocess.run(command, capture_output=True, check=False, text=True)

    assert first.returncode == 0, first.stderr
    assert first.stdout.startswith("SHADOW_CYCLE_INPUT_RECORDED cycle=tqqq_core_only_p2_v5_shadow_20260820")
    assert shadow_ledger.validate_shadow_cycle_input(json.loads(output_path.read_text(encoding="utf-8"))) == expected
    assert second.returncode == 1
    assert "File exists" in second.stderr
