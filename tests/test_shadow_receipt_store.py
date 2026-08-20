from __future__ import annotations

import copy
import hashlib
import json

import pytest

from alpaca_platform import shadow_ledger, shadow_receipt_store, shadow_risk_gate_decision, shadow_scheduler


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


def forward_observation(*, allocation: dict[str, int] | None = None) -> dict[str, object]:
    decision: dict[str, object] = {
        "decision_id": "tqqq_core_only_p2_v5_20260820",
        "effective_session": "2026-08-20",
        "producer_revision": revision("a"),
        "allocation_bps": allocation or {"TQQQ": 4_500, "QQQM": 4_500, "BOXX": 800, "CASH": 200},
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


def ready_outcome(*, allocation: dict[str, int] | None = None) -> shadow_scheduler.ShadowCycleOutcome:
    return shadow_scheduler.run_tqqq_shadow_cycle(
        cycle_id="tqqq_core_only_p2_v5_shadow_20260820",
        computed_at="2026-08-20T20:00:00Z",
        forward_observation=forward_observation(allocation=allocation),
        policy_gate_receipt=policy_gate_receipt(),
        risk_control={
            "stage": "SHADOW",
            "execution_lane": "SHADOW_LEDGER",
            "risk_policy_id": "tqqq-core-only-shadow",
            "risk_policy_version": "v1",
            "risk_policy_sha256": sha("f"),
        },
        deployment_bundle_sha256=sha("1"),
    )


def risk_gate_decision_envelope(
    outcome: shadow_scheduler.ShadowCycleOutcome,
    *,
    decision: str = "ALLOW_NEW_RISK",
    source_risk_policy_sha256: str | None = None,
) -> dict[str, object]:
    assert outcome.receipt is not None
    risk_decision: dict[str, object] = {
        "schema": shadow_risk_gate_decision.DETERMINISTIC_RISK_GATE_DECISION_SCHEMA,
        "evaluation_id": outcome.result["cycle_id"],
        "observed_at": outcome.result["computed_at"],
        "policy": {
            "risk_policy_id": "tqqq-core-only-shadow-risk-gate",
            "risk_policy_version": "v1",
            "risk_policy_sha256": sha("9"),
        },
        "decision": decision,
        "reason_codes": [] if decision == "ALLOW_NEW_RISK" else ["CIRCUIT_BREAKER_OPEN"],
        "next_circuit_breaker_state": "CLOSED" if decision == "ALLOW_NEW_RISK" else "OPEN",
        "manual_reset_required": True,
        "projected": {
            "gross_notional_cents": 50_000,
            "symbol_gross_notional_cents": 50_000,
            "strategy_gross_notional_cents": 50_000,
            "leverage_bps": 5_000,
            "decisions_in_session": 1,
        },
        "decision_sha256": "",
    }
    canonical_decision = dict(risk_decision)
    canonical_decision.pop("decision_sha256")
    risk_decision["decision_sha256"] = hashlib.sha256(
        json.dumps(canonical_decision, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("utf-8")
    ).hexdigest()
    source_risk_control = {
        "schema": shadow_risk_gate_decision.FORWARD_OBSERVATION_RISK_CONTROL_SCHEMA,
        "risk_policy_id": outcome.receipt["risk_control"]["risk_policy_id"],
        "risk_policy_version": outcome.receipt["risk_control"]["risk_policy_version"],
        "risk_policy_sha256": source_risk_policy_sha256
        or outcome.receipt["risk_control"]["risk_policy_sha256"],
    }
    envelope: dict[str, object] = {
        "schema": shadow_risk_gate_decision.P5_RISK_GATE_DECISION_ENVELOPE_SCHEMA,
        "cycle_id": outcome.result["cycle_id"],
        "computed_at": outcome.result["computed_at"],
        "source_risk_control": source_risk_control,
        "risk_gate_decision": risk_decision,
        "envelope_sha256": "",
    }
    envelope["envelope_sha256"] = shadow_risk_gate_decision.calculate_p5_risk_gate_decision_envelope_sha256(
        envelope
    )
    return envelope


def test_ready_controller_outcome_becomes_a_closed_sanitized_admission():
    outcome = ready_outcome()

    admission = shadow_receipt_store.build_shadow_receipt_admission(
        outcome,
        risk_gate_decision=risk_gate_decision_envelope(outcome),
    )

    assert shadow_receipt_store.validate_shadow_receipt_admission(admission) == admission
    assert admission["scheduler_result"] == outcome.result
    assert admission["shadow_receipt"] == outcome.receipt
    assert admission["risk_gate_decision"]["decision_sha256"] == risk_gate_decision_envelope(outcome)[
        "risk_gate_decision"
    ]["decision_sha256"]
    rendered = json.dumps(admission, sort_keys=True).lower()
    for forbidden in ("broker", "order", "account", "notional", "price", "credential"):
        assert forbidden not in rendered

    tampered = copy.deepcopy(admission)
    tampered["shadow_receipt"]["forward_decision"]["allocation_bps"]["TQQQ"] = 0
    with pytest.raises(shadow_receipt_store.ShadowReceiptStoreError, match="ledger receipt"):
        shadow_receipt_store.validate_shadow_receipt_admission(tampered)


def test_in_memory_store_is_create_only_and_reconciles_identical_admission():
    store = shadow_receipt_store.InMemoryShadowReceiptStore()
    outcome = ready_outcome()
    risk_gate_decision = risk_gate_decision_envelope(outcome)

    first = shadow_receipt_store.persist_shadow_cycle_outcome(
        outcome, store, risk_gate_decision=risk_gate_decision
    )
    second = shadow_receipt_store.persist_shadow_cycle_outcome(
        outcome, store, risk_gate_decision=risk_gate_decision
    )

    assert first["status"] == "RECORDED"
    assert second["status"] == "RECONCILED"
    assert first["shadow_receipt_sha256"] == second["shadow_receipt_sha256"] == outcome.receipt["receipt_sha256"]
    stored = store.read(outcome.result["cycle_id"])
    assert stored is not None
    stored["shadow_receipt"]["cycle_id"] = "altered"
    assert store.read(outcome.result["cycle_id"])["shadow_receipt"]["cycle_id"] == outcome.result["cycle_id"]


def test_missing_scheduler_prerequisite_stays_parked_and_never_calls_store():
    store = shadow_receipt_store.InMemoryShadowReceiptStore()
    outcome = shadow_scheduler.run_tqqq_shadow_cycle(
        cycle_id="tqqq_core_only_p2_v5_shadow_20260820",
        computed_at="2026-08-20T20:00:00Z",
        forward_observation=None,
        policy_gate_receipt=None,
        risk_control=None,
        deployment_bundle_sha256=None,
    )

    result = shadow_receipt_store.persist_shadow_cycle_outcome(outcome, store)

    assert result["status"] == "PARKED"
    assert result["reason_code"] == "forward_observation_missing"
    assert store.read(outcome.result["cycle_id"]) is None
    with pytest.raises(shadow_receipt_store.ShadowReceiptStoreError, match="RECEIPT_READY"):
        shadow_receipt_store.build_shadow_receipt_admission(outcome, risk_gate_decision={})


def test_conflicting_cycle_is_parked_without_overwriting_existing_admission():
    store = shadow_receipt_store.InMemoryShadowReceiptStore()
    first_outcome = ready_outcome()
    first_result = shadow_receipt_store.persist_shadow_cycle_outcome(
        first_outcome,
        store,
        risk_gate_decision=risk_gate_decision_envelope(first_outcome),
    )
    conflicting_outcome = ready_outcome(allocation={"TQQQ": 0, "QQQM": 9_000, "BOXX": 800, "CASH": 200})

    result = shadow_receipt_store.persist_shadow_cycle_outcome(
        conflicting_outcome,
        store,
        risk_gate_decision=risk_gate_decision_envelope(conflicting_outcome),
    )

    assert result["status"] == "PARKED"
    assert result["reason_code"] == "receipt_conflict"
    stored = store.read(first_outcome.result["cycle_id"])
    assert stored is not None
    assert stored["admission_sha256"] == first_result["admission_sha256"]


@pytest.mark.parametrize(
    ("risk_gate_decision", "reason_code"),
    [
        (None, "risk_gate_decision_missing"),
        ({}, "risk_gate_decision_invalid"),
    ],
)
def test_missing_or_invalid_risk_decision_is_parked_without_touching_store(
    risk_gate_decision: object | None,
    reason_code: str,
):
    store = shadow_receipt_store.InMemoryShadowReceiptStore()
    outcome = ready_outcome()

    result = shadow_receipt_store.persist_shadow_cycle_outcome(
        outcome,
        store,
        risk_gate_decision=risk_gate_decision,
    )

    assert result["status"] == "PARKED"
    assert result["reason_code"] == reason_code
    assert store.read(outcome.result["cycle_id"]) is None


def test_prohibited_or_mismatched_risk_decision_is_parked_without_touching_store():
    store = shadow_receipt_store.InMemoryShadowReceiptStore()
    outcome = ready_outcome()
    invalid_digest = risk_gate_decision_envelope(outcome)
    invalid_digest["risk_gate_decision"]["decision_sha256"] = sha("0")
    invalid_digest["envelope_sha256"] = shadow_risk_gate_decision.calculate_p5_risk_gate_decision_envelope_sha256(
        invalid_digest
    )

    invalid = shadow_receipt_store.persist_shadow_cycle_outcome(
        outcome,
        store,
        risk_gate_decision=invalid_digest,
    )
    prohibited = shadow_receipt_store.persist_shadow_cycle_outcome(
        outcome,
        store,
        risk_gate_decision=risk_gate_decision_envelope(outcome, decision="NEW_RISK_PROHIBITED"),
    )
    mismatched = shadow_receipt_store.persist_shadow_cycle_outcome(
        outcome,
        store,
        risk_gate_decision=risk_gate_decision_envelope(outcome, source_risk_policy_sha256=sha("0")),
    )

    assert invalid["status"] == "PARKED"
    assert invalid["reason_code"] == "risk_gate_decision_invalid"
    assert prohibited["status"] == "PARKED"
    assert prohibited["reason_code"] == "risk_gate_decision_prohibited"
    assert mismatched["status"] == "PARKED"
    assert mismatched["reason_code"] == "risk_gate_decision_mismatch"
    assert store.read(outcome.result["cycle_id"]) is None
