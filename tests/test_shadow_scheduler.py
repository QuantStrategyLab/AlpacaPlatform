from __future__ import annotations

import copy
import json

import pytest

from alpaca_platform import shadow_ledger, shadow_scheduler


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


def forward_observation() -> dict[str, object]:
    decision: dict[str, object] = {
        "decision_id": "tqqq_core_only_p2_v5_20260820",
        "effective_session": "2026-08-20",
        "producer_revision": revision("a"),
        "allocation_bps": {"TQQQ": 4_500, "QQQM": 4_500, "BOXX": 800, "CASH": 200},
        "decision_sha256": "",
    }
    decision["decision_sha256"] = shadow_ledger.calculate_decision_sha256(decision)
    observation: dict[str, object] = {
        "schema": shadow_ledger.FORWARD_OBSERVATION_SCHEMA,
        "produced_at": "2026-08-20T19:30:00Z",
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
        "forward_observation_sha256": "",
    }
    observation["forward_observation_sha256"] = shadow_ledger.calculate_forward_observation_sha256(observation)
    return observation


def valid_request() -> dict[str, object]:
    return {
        "cycle_id": "tqqq_core_only_p2_v5_shadow_20260820",
        "computed_at": "2026-08-20T20:00:00Z",
        "forward_observation": forward_observation(),
        "policy_gate_receipt": policy_gate_receipt(),
        "risk_control": {
            "stage": "SHADOW",
            "execution_lane": "SHADOW_LEDGER",
            "risk_policy_id": "tqqq-core-only-shadow",
            "risk_policy_version": "v1",
            "risk_policy_sha256": sha("f"),
        },
        "deployment_bundle_sha256": sha("1"),
    }


def test_complete_pre_authorized_inputs_produce_one_unpersisted_virtual_receipt():
    outcome = shadow_scheduler.run_tqqq_shadow_cycle(**valid_request())

    assert outcome.result == {
        "schema": shadow_scheduler.SCHEDULER_RESULT_SCHEMA,
        "cycle_id": "tqqq_core_only_p2_v5_shadow_20260820",
        "computed_at": "2026-08-20T20:00:00Z",
        "status": "RECEIPT_READY",
        "reason_code": "receipt_ready",
        "shadow_receipt_sha256": outcome.receipt["receipt_sha256"],
    }
    assert outcome.receipt is not None
    assert shadow_ledger.validate_shadow_ledger_receipt(outcome.receipt) == outcome.receipt
    assert outcome.receipt["shadow_adjustments_bps"] == {"TQQQ": 4_500, "QQQM": 4_500, "BOXX": 800, "CASH": -9_800}


@pytest.mark.parametrize(
    ("field", "reason_code"),
    [
        ("forward_observation", "forward_observation_missing"),
        ("policy_gate_receipt", "policy_gate_receipt_missing"),
        ("risk_control", "risk_control_missing"),
        ("deployment_bundle_sha256", "deployment_bundle_missing"),
    ],
)
def test_missing_prerequisites_are_parked_without_a_receipt(field: str, reason_code: str):
    request = valid_request()
    request[field] = None

    outcome = shadow_scheduler.run_tqqq_shadow_cycle(**request)

    assert outcome.receipt is None
    assert outcome.result["status"] == "PARKED"
    assert outcome.result["reason_code"] == reason_code
    assert outcome.result["shadow_receipt_sha256"] is None


def test_invalid_upstream_or_parent_is_parked_without_leaking_input_errors():
    invalid_observation = valid_request()
    invalid_observation["forward_observation"]["credential"] = "do-not-publish"
    outcome = shadow_scheduler.run_tqqq_shadow_cycle(**invalid_observation)
    assert outcome.result["reason_code"] == "cycle_input_invalid"
    assert "credential" not in json.dumps(outcome.result).lower()

    invalid_parent = valid_request()
    invalid_parent["prior_receipt"] = {"not": "a-receipt"}
    outcome = shadow_scheduler.run_tqqq_shadow_cycle(**invalid_parent)
    assert outcome.result["reason_code"] == "prior_receipt_invalid"


def test_later_cycle_links_to_a_valid_prior_receipt():
    first = shadow_scheduler.run_tqqq_shadow_cycle(**valid_request()).receipt
    request = valid_request()
    request["cycle_id"] = "tqqq_core_only_p2_v5_shadow_20260821"
    request["computed_at"] = "2026-08-21T20:00:00Z"
    request["forward_observation"] = copy.deepcopy(request["forward_observation"])
    request["forward_observation"]["produced_at"] = "2026-08-21T19:30:00Z"
    request["forward_observation"]["forward_decision"]["effective_session"] = "2026-08-21"
    request["forward_observation"]["forward_decision"]["decision_id"] = "tqqq_core_only_p2_v5_20260821"
    request["forward_observation"]["forward_decision"]["decision_sha256"] = shadow_ledger.calculate_decision_sha256(
        request["forward_observation"]["forward_decision"]
    )
    request["forward_observation"]["forward_observation_sha256"] = shadow_ledger.calculate_forward_observation_sha256(
        request["forward_observation"]
    )
    request["policy_gate_receipt"] = copy.deepcopy(request["policy_gate_receipt"])
    request["policy_gate_receipt"]["policy"]["effective_at"] = "2026-08-21T18:00:00Z"
    request["policy_gate_receipt"]["policy"]["expires_at"] = "2026-08-22T18:00:00Z"
    request["policy_gate_receipt"]["activation"]["effective_at"] = "2026-08-21T19:00:00Z"
    request["policy_gate_receipt"]["activation"]["expires_at"] = "2026-08-22T17:00:00Z"
    request["policy_gate_receipt"]["trusted_policy_root"]["expires_at"] = "2026-08-23T00:00:00Z"
    request["policy_gate_receipt"]["receipt_sha256"] = shadow_ledger.calculate_policy_gate_receipt_sha256(
        request["policy_gate_receipt"]
    )
    request["prior_receipt"] = first

    outcome = shadow_scheduler.run_tqqq_shadow_cycle(**request)

    assert outcome.result["status"] == "RECEIPT_READY"
    assert outcome.receipt["ledger_parent_sha256"] == first["receipt_sha256"]
