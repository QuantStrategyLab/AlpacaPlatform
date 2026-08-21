from __future__ import annotations

import copy
import hashlib
import json

from alpaca_platform import (
    InMemoryRestrictedP5ShadowArtifactReader,
    InMemoryShadowReceiptStore,
    P5DefaultParkedSchedulerError,
    P5ShadowArtifactSnapshot,
    RestrictedP5ArtifactReadError,
    calculate_forward_observation_sha256,
    calculate_p5_default_parked_scheduler_deduplication_sha256,
    calculate_p5_risk_gate_decision_envelope_sha256,
    calculate_policy_gate_receipt_sha256,
    run_p5_default_parked_shadow_cycle,
    shadow_ledger,
    shadow_risk_gate_decision,
    summarize_p5_default_parked_scheduler_status,
    validate_p5_default_parked_scheduler_status,
    validate_p5_default_parked_scheduler_summary,
)


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
    receipt["receipt_sha256"] = calculate_policy_gate_receipt_sha256(receipt)
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
    observation["forward_observation_sha256"] = calculate_forward_observation_sha256(observation)
    return observation


def risk_control() -> dict[str, str]:
    return {
        "stage": "SHADOW",
        "execution_lane": "SHADOW_LEDGER",
        "risk_policy_id": "tqqq-core-only-shadow",
        "risk_policy_version": "v1",
        "risk_policy_sha256": sha("f"),
    }


def risk_gate_decision_envelope() -> dict[str, object]:
    cycle_id = "tqqq_core_only_p2_v5_shadow_20260820"
    computed_at = "2026-08-20T20:00:00Z"
    decision: dict[str, object] = {
        "schema": shadow_risk_gate_decision.DETERMINISTIC_RISK_GATE_DECISION_SCHEMA,
        "evaluation_id": cycle_id,
        "observed_at": computed_at,
        "policy": {
            "risk_policy_id": "tqqq-core-only-shadow-risk-gate",
            "risk_policy_version": "v1",
            "risk_policy_sha256": sha("9"),
        },
        "decision": "ALLOW_NEW_RISK",
        "reason_codes": [],
        "next_circuit_breaker_state": "CLOSED",
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
    canonical_decision = dict(decision)
    canonical_decision.pop("decision_sha256")
    decision["decision_sha256"] = hashlib.sha256(
        json.dumps(canonical_decision, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("utf-8")
    ).hexdigest()
    envelope: dict[str, object] = {
        "schema": shadow_risk_gate_decision.P5_RISK_GATE_DECISION_ENVELOPE_SCHEMA,
        "cycle_id": cycle_id,
        "computed_at": computed_at,
        "source_risk_control": {
            "schema": shadow_risk_gate_decision.FORWARD_OBSERVATION_RISK_CONTROL_SCHEMA,
            "risk_policy_id": "tqqq-core-only-shadow",
            "risk_policy_version": "v1",
            "risk_policy_sha256": sha("f"),
        },
        "risk_gate_decision": decision,
        "envelope_sha256": "",
    }
    envelope["envelope_sha256"] = calculate_p5_risk_gate_decision_envelope_sha256(envelope)
    return envelope


def ready_snapshot() -> P5ShadowArtifactSnapshot:
    return P5ShadowArtifactSnapshot(
        cycle_id="tqqq_core_only_p2_v5_shadow_20260820",
        forward_observation=forward_observation(),
        policy_gate_receipt=policy_gate_receipt(),
        risk_control=risk_control(),
        deployment_bundle_sha256=sha("1"),
        risk_gate_decision=risk_gate_decision_envelope(),
    )


class SpyStore(InMemoryShadowReceiptStore):
    def __init__(self) -> None:
        super().__init__()
        self.read_count = 0
        self.create_count = 0

    def read(self, cycle_id: str):  # type: ignore[no-untyped-def]
        self.read_count += 1
        return super().read(cycle_id)

    def create_if_absent(self, admission):  # type: ignore[no-untyped-def]
        self.create_count += 1
        return super().create_if_absent(admission)


def test_default_without_reader_is_parked_and_never_touches_store():
    store = SpyStore()

    outcome = run_p5_default_parked_shadow_cycle(
        cycle_id="tqqq_core_only_p2_v5_shadow_20260820",
        computed_at="2026-08-20T20:00:00Z",
        receipt_store=store,
    )

    assert outcome.status["status"] == "PARKED"
    assert outcome.status["reason_code"] == "artifact_reader_missing"
    assert outcome.status["shadow_receipt_sha256"] is None
    assert outcome.status["admission_sha256"] is None
    assert store.read_count == store.create_count == 0
    assert validate_p5_default_parked_scheduler_summary(outcome.summary) == outcome.summary


def test_missing_or_inconsistent_snapshot_is_parked_without_store_access():
    reader = InMemoryRestrictedP5ShadowArtifactReader()
    store = SpyStore()
    missing = run_p5_default_parked_shadow_cycle(
        cycle_id="tqqq_core_only_p2_v5_shadow_20260820",
        computed_at="2026-08-20T20:00:00Z",
        artifact_reader=reader,
        receipt_store=store,
    )

    class InconsistentReader:
        def read_snapshot(self, *, cycle_id: str) -> P5ShadowArtifactSnapshot | None:
            return P5ShadowArtifactSnapshot(
                cycle_id="tqqq_core_only_p2_v5_shadow_20260821",
                forward_observation=None,
                policy_gate_receipt=None,
                risk_control=None,
                deployment_bundle_sha256=None,
                risk_gate_decision=None,
            )

    invalid = run_p5_default_parked_shadow_cycle(
        cycle_id="tqqq_core_only_p2_v5_shadow_20260820",
        computed_at="2026-08-20T20:00:00Z",
        artifact_reader=InconsistentReader(),
        receipt_store=store,
    )

    assert missing.status["reason_code"] == "artifact_snapshot_missing"
    assert invalid.status["reason_code"] == "artifact_snapshot_invalid"
    assert store.read_count == store.create_count == 0


def test_invalid_prior_receipt_or_risk_envelope_is_parked_before_store_access():
    reader = InMemoryRestrictedP5ShadowArtifactReader()
    invalid_prior = ready_snapshot()
    invalid_prior = P5ShadowArtifactSnapshot(**{**invalid_prior.__dict__, "prior_receipt": {}})
    reader.put_snapshot(invalid_prior)
    store = SpyStore()

    prior_result = run_p5_default_parked_shadow_cycle(
        cycle_id=invalid_prior.cycle_id,
        computed_at="2026-08-20T20:00:00Z",
        artifact_reader=reader,
        receipt_store=store,
    )
    invalid_envelope = ready_snapshot()
    invalid_envelope = P5ShadowArtifactSnapshot(**{**invalid_envelope.__dict__, "risk_gate_decision": {}})
    reader.put_snapshot(invalid_envelope)
    envelope_result = run_p5_default_parked_shadow_cycle(
        cycle_id=invalid_envelope.cycle_id,
        computed_at="2026-08-20T20:00:00Z",
        artifact_reader=reader,
        receipt_store=store,
    )

    assert prior_result.status["reason_code"] == "prior_receipt_invalid"
    assert envelope_result.status["reason_code"] == "risk_gate_decision_invalid"
    assert store.read_count == store.create_count == 0


def test_complete_snapshot_records_once_then_reconciles_with_sanitized_dedup_summary():
    reader = InMemoryRestrictedP5ShadowArtifactReader()
    snapshot = ready_snapshot()
    reader.put_snapshot(snapshot)
    store = SpyStore()

    first = run_p5_default_parked_shadow_cycle(
        cycle_id=snapshot.cycle_id,
        computed_at="2026-08-20T20:00:00Z",
        artifact_reader=reader,
        receipt_store=store,
    )
    second = run_p5_default_parked_shadow_cycle(
        cycle_id=snapshot.cycle_id,
        computed_at="2026-08-20T20:00:00Z",
        artifact_reader=reader,
        receipt_store=store,
    )

    assert first.status["status"] == "RECORDED"
    assert second.status["status"] == "RECONCILED"
    assert first.status["shadow_receipt_sha256"] is not None
    assert first.summary["cycle_id"] == snapshot.cycle_id
    assert first.summary["deduplication_sha256"] == first.status["deduplication_sha256"]
    assert second.summary["deduplication_sha256"] != first.summary["deduplication_sha256"]
    assert reader.read_count == 2
    assert store.create_count == 1
    rendered = json.dumps(first.status, sort_keys=True).lower()
    for forbidden in ("broker", "order", "account", "notional", "price", "credential"):
        assert forbidden not in rendered


def test_unavailable_reader_is_parked_without_exposing_adapter_error_or_using_store():
    class UnavailableReader:
        def read_snapshot(self, *, cycle_id: str) -> P5ShadowArtifactSnapshot | None:
            raise RestrictedP5ArtifactReadError("do not publish this adapter detail")

    store = SpyStore()
    outcome = run_p5_default_parked_shadow_cycle(
        cycle_id="tqqq_core_only_p2_v5_shadow_20260820",
        computed_at="2026-08-20T20:00:00Z",
        artifact_reader=UnavailableReader(),
        receipt_store=store,
    )

    assert outcome.status["reason_code"] == "artifact_snapshot_unavailable"
    assert store.read_count == store.create_count == 0


def test_status_digest_rejects_tampering_and_summary_is_derived_only_from_status():
    status = run_p5_default_parked_shadow_cycle(
        cycle_id="tqqq_core_only_p2_v5_shadow_20260820",
        computed_at="2026-08-20T20:00:00Z",
    ).status
    tampered = copy.deepcopy(status)
    tampered["reason_code"] = "receipt_conflict"

    assert status["deduplication_sha256"] == calculate_p5_default_parked_scheduler_deduplication_sha256(status)
    assert summarize_p5_default_parked_scheduler_status(status)["cycle_id"] == status["cycle_id"]
    try:
        validate_p5_default_parked_scheduler_status(tampered)
    except P5DefaultParkedSchedulerError as exc:
        assert "deduplication_sha256" in str(exc)
    else:  # pragma: no cover - documents the required closed digest behavior
        raise AssertionError("tampered scheduler status unexpectedly validated")
