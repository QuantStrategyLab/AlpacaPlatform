"""Closed adapter boundary for a QSL deterministic risk decision in P5.

The QSL risk kernel remains owned by ``QuantRuntimeSettings``.  This module
does not evaluate exposure, connect to that repository, fetch an account, or
create an order.  It only validates one complete, already-produced decision
and its small P5 envelope before reducing it to a non-sensitive reference that
may be included in a shadow-receipt admission.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

DETERMINISTIC_RISK_GATE_DECISION_SCHEMA = "qsl.deterministic_risk_gate_decision.v1"
FORWARD_OBSERVATION_RISK_CONTROL_SCHEMA = "qsl.forward_observation_risk_control.v1"
P5_RISK_GATE_DECISION_ENVELOPE_SCHEMA = "qsl.tqqq_shadow_risk_gate_decision_envelope.v1"
P5_RISK_GATE_DECISION_REFERENCE_SCHEMA = "qsl.tqqq_shadow_risk_gate_decision_reference.v1"

_IDENTITY = re.compile(r"^[a-z][a-z0-9]*(?:[._-][a-z0-9]+)*$")
_DIGEST = re.compile(r"^[0-9a-f]{64}$")
_TIMESTAMP = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z$")
_REASON_CODE = re.compile(r"^[A-Z][A-Z0-9_]*$")
_ENVELOPE_FIELDS = {
    "schema",
    "cycle_id",
    "computed_at",
    "source_risk_control",
    "risk_gate_decision",
    "envelope_sha256",
}
_RISK_CONTROL_FIELDS = {"schema", "risk_policy_id", "risk_policy_version", "risk_policy_sha256"}
_DECISION_FIELDS = {
    "schema",
    "evaluation_id",
    "observed_at",
    "policy",
    "decision",
    "reason_codes",
    "next_circuit_breaker_state",
    "manual_reset_required",
    "projected",
    "decision_sha256",
}
_POLICY_FIELDS = {"risk_policy_id", "risk_policy_version", "risk_policy_sha256"}
_PROJECTED_FIELDS = {
    "gross_notional_cents",
    "symbol_gross_notional_cents",
    "strategy_gross_notional_cents",
    "leverage_bps",
    "decisions_in_session",
}
_REFERENCE_FIELDS = {
    "schema",
    "source_risk_control",
    "source_decision_schema",
    "evaluation_id",
    "observed_at",
    "risk_gate_policy",
    "decision_sha256",
}


class ShadowRiskGateDecisionError(ValueError):
    """Raised when a QSL risk-decision envelope is structurally unsafe."""


class ShadowRiskGateDecisionMismatchError(ShadowRiskGateDecisionError):
    """Raised when one decision is not bound to this P5 cycle and policy."""


class ShadowRiskGateDecisionProhibitedError(ShadowRiskGateDecisionError):
    """Raised when a valid risk decision prohibits additional risk."""


def _fail(message: str) -> None:
    raise ShadowRiskGateDecisionError(message)


def _exact_keys(value: Mapping[str, Any], expected: set[str], label: str) -> None:
    missing = sorted(expected - set(value))
    unknown = sorted(set(value) - expected)
    if missing:
        _fail(f"{label} missing required field(s): {', '.join(missing)}")
    if unknown:
        _fail(f"{label} has unknown field(s): {', '.join(unknown)}")


def _object(value: Any, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        _fail(f"{label} must be an object")
    return value


def _identity(value: Any, label: str) -> str:
    if not isinstance(value, str) or not _IDENTITY.fullmatch(value):
        _fail(f"{label} must be a lowercase immutable identity")
    return value


def _digest(value: Any, label: str) -> str:
    if not isinstance(value, str) or not _DIGEST.fullmatch(value):
        _fail(f"{label} must be a lowercase SHA-256 digest")
    return value


def _timestamp(value: Any, label: str) -> str:
    if not isinstance(value, str) or not _TIMESTAMP.fullmatch(value):
        _fail(f"{label} must be an RFC3339 UTC timestamp with whole seconds")
    try:
        datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
    except ValueError as exc:
        raise ShadowRiskGateDecisionError(f"{label} must be a valid calendar timestamp") from exc
    return value


def _nonnegative_integer(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        _fail(f"{label} must be a non-negative integer")
    return value


def _canonical_json(value: Mapping[str, Any], excluded_field: str, label: str) -> str:
    content = dict(value)
    content.pop(excluded_field, None)
    try:
        return json.dumps(content, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise ShadowRiskGateDecisionError(f"{label} cannot be represented as canonical JSON") from exc


def calculate_p5_risk_gate_decision_envelope_sha256(value: Mapping[str, Any]) -> str:
    """Return the stable digest for the transient P5 risk-decision envelope."""
    return hashlib.sha256(
        _canonical_json(value, "envelope_sha256", "P5 risk-gate decision envelope").encode("utf-8")
    ).hexdigest()


def _calculate_source_decision_sha256(value: Mapping[str, Any]) -> str:
    """Recompute the QSL V1 decision digest without importing its kernel."""
    return hashlib.sha256(
        _canonical_json(value, "decision_sha256", "deterministic risk-gate decision").encode("utf-8")
    ).hexdigest()


def _risk_control_reference(value: Any, label: str) -> dict[str, str]:
    reference = _object(value, label)
    _exact_keys(reference, _RISK_CONTROL_FIELDS, label)
    if reference["schema"] != FORWARD_OBSERVATION_RISK_CONTROL_SCHEMA:
        _fail(f"{label}.schema must be {FORWARD_OBSERVATION_RISK_CONTROL_SCHEMA}")
    return {
        "schema": FORWARD_OBSERVATION_RISK_CONTROL_SCHEMA,
        "risk_policy_id": _identity(reference["risk_policy_id"], f"{label}.risk_policy_id"),
        "risk_policy_version": _identity(reference["risk_policy_version"], f"{label}.risk_policy_version"),
        "risk_policy_sha256": _digest(reference["risk_policy_sha256"], f"{label}.risk_policy_sha256"),
    }


def _policy_reference(value: Any, label: str) -> dict[str, str]:
    policy = _object(value, label)
    _exact_keys(policy, _POLICY_FIELDS, label)
    return {
        "risk_policy_id": _identity(policy["risk_policy_id"], f"{label}.risk_policy_id"),
        "risk_policy_version": _identity(policy["risk_policy_version"], f"{label}.risk_policy_version"),
        "risk_policy_sha256": _digest(policy["risk_policy_sha256"], f"{label}.risk_policy_sha256"),
    }


def _validated_allow_decision(value: Any, *, cycle_id: str, computed_at: str) -> dict[str, Any]:
    decision = _object(value, "P5 risk-gate decision")
    _exact_keys(decision, _DECISION_FIELDS, "P5 risk-gate decision")
    if decision["schema"] != DETERMINISTIC_RISK_GATE_DECISION_SCHEMA:
        _fail(f"P5 risk-gate decision.schema must be {DETERMINISTIC_RISK_GATE_DECISION_SCHEMA}")
    evaluation_id = _identity(decision["evaluation_id"], "P5 risk-gate decision.evaluation_id")
    if evaluation_id != cycle_id:
        raise ShadowRiskGateDecisionMismatchError("risk-gate decision evaluation_id must match P5 cycle_id")
    observed_at = _timestamp(decision["observed_at"], "P5 risk-gate decision.observed_at")
    if observed_at != computed_at:
        raise ShadowRiskGateDecisionMismatchError("risk-gate decision observed_at must match P5 computed_at")
    policy = _policy_reference(decision["policy"], "P5 risk-gate decision.policy")
    if decision["decision"] not in {"ALLOW_NEW_RISK", "NEW_RISK_PROHIBITED"}:
        _fail("P5 risk-gate decision.decision must be ALLOW_NEW_RISK or NEW_RISK_PROHIBITED")
    if not isinstance(decision["reason_codes"], list) or not all(
        isinstance(reason, str) and _REASON_CODE.fullmatch(reason) for reason in decision["reason_codes"]
    ):
        _fail("P5 risk-gate decision.reason_codes must be a list of stable reason codes")
    if decision["decision"] == "ALLOW_NEW_RISK":
        if decision["reason_codes"] != []:
            _fail("an ALLOW_NEW_RISK decision must have no reason codes")
        if decision["next_circuit_breaker_state"] != "CLOSED":
            _fail("an ALLOW_NEW_RISK decision must keep the circuit breaker closed")
    else:
        if not decision["reason_codes"]:
            _fail("a NEW_RISK_PROHIBITED decision must include a reason code")
        if decision["next_circuit_breaker_state"] != "OPEN":
            _fail("a NEW_RISK_PROHIBITED decision must open the circuit breaker")
    if decision["manual_reset_required"] is not True:
        _fail("P5 risk-gate decision.manual_reset_required must be true")
    projected = _object(decision["projected"], "P5 risk-gate decision.projected")
    _exact_keys(projected, _PROJECTED_FIELDS, "P5 risk-gate decision.projected")
    for field in _PROJECTED_FIELDS:
        _nonnegative_integer(projected[field], f"P5 risk-gate decision.projected.{field}")
    decision_sha256 = _digest(decision["decision_sha256"], "P5 risk-gate decision.decision_sha256")
    if decision_sha256 != _calculate_source_decision_sha256(decision):
        _fail("P5 risk-gate decision.decision_sha256 mismatch")
    if decision["decision"] == "NEW_RISK_PROHIBITED":
        raise ShadowRiskGateDecisionProhibitedError("risk-gate decision prohibits new risk")
    return {
        "schema": DETERMINISTIC_RISK_GATE_DECISION_SCHEMA,
        "evaluation_id": evaluation_id,
        "observed_at": observed_at,
        "policy": policy,
        "decision_sha256": decision_sha256,
    }


def validate_p5_risk_gate_decision_reference(
    value: Any,
    *,
    expected_source_risk_control: Mapping[str, str],
    expected_computed_at: str,
) -> dict[str, Any]:
    """Validate the non-sensitive reference persisted inside a P5 admission."""
    reference = _object(value, "P5 risk-gate decision reference")
    _exact_keys(reference, _REFERENCE_FIELDS, "P5 risk-gate decision reference")
    if reference["schema"] != P5_RISK_GATE_DECISION_REFERENCE_SCHEMA:
        _fail(f"P5 risk-gate decision reference.schema must be {P5_RISK_GATE_DECISION_REFERENCE_SCHEMA}")
    source_risk_control = _risk_control_reference(
        reference["source_risk_control"], "P5 risk-gate decision reference.source_risk_control"
    )
    expected = {
        "schema": FORWARD_OBSERVATION_RISK_CONTROL_SCHEMA,
        "risk_policy_id": expected_source_risk_control["risk_policy_id"],
        "risk_policy_version": expected_source_risk_control["risk_policy_version"],
        "risk_policy_sha256": expected_source_risk_control["risk_policy_sha256"],
    }
    if source_risk_control != expected:
        raise ShadowRiskGateDecisionMismatchError(
            "risk-gate decision source_risk_control does not match shadow receipt risk_control"
        )
    if reference["source_decision_schema"] != DETERMINISTIC_RISK_GATE_DECISION_SCHEMA:
        _fail(
            "P5 risk-gate decision reference.source_decision_schema must be "
            f"{DETERMINISTIC_RISK_GATE_DECISION_SCHEMA}"
        )
    evaluation_id = _identity(reference["evaluation_id"], "P5 risk-gate decision reference.evaluation_id")
    observed_at = _timestamp(reference["observed_at"], "P5 risk-gate decision reference.observed_at")
    if observed_at != expected_computed_at:
        raise ShadowRiskGateDecisionMismatchError(
            "risk-gate decision reference observed_at must match shadow receipt computed_at"
        )
    return {
        "schema": P5_RISK_GATE_DECISION_REFERENCE_SCHEMA,
        "source_risk_control": source_risk_control,
        "source_decision_schema": DETERMINISTIC_RISK_GATE_DECISION_SCHEMA,
        "evaluation_id": evaluation_id,
        "observed_at": observed_at,
        "risk_gate_policy": _policy_reference(
            reference["risk_gate_policy"], "P5 risk-gate decision reference.risk_gate_policy"
        ),
        "decision_sha256": _digest(reference["decision_sha256"], "P5 risk-gate decision reference.decision_sha256"),
    }


def build_p5_risk_gate_decision_reference(
    envelope: Any,
    *,
    expected_cycle_id: str,
    expected_computed_at: str,
    expected_source_risk_control: Mapping[str, str],
) -> dict[str, Any]:
    """Reduce one verified QSL decision to the P5-safe, admission-only reference.

    The envelope is intentionally transient: QSL's full decision contains
    projected monetary values, so P5 persists only its policy and digest
    bindings.  No decision can be reused for another cycle, timestamp, or
    forward-observation risk-control triple.
    """
    envelope_value = _object(envelope, "P5 risk-gate decision envelope")
    _exact_keys(envelope_value, _ENVELOPE_FIELDS, "P5 risk-gate decision envelope")
    if envelope_value["schema"] != P5_RISK_GATE_DECISION_ENVELOPE_SCHEMA:
        _fail(
            "P5 risk-gate decision envelope.schema must be " f"{P5_RISK_GATE_DECISION_ENVELOPE_SCHEMA}"
        )
    cycle_id = _identity(envelope_value["cycle_id"], "P5 risk-gate decision envelope.cycle_id")
    if cycle_id != expected_cycle_id:
        raise ShadowRiskGateDecisionMismatchError("risk-gate decision envelope cycle_id does not match P5 cycle")
    computed_at = _timestamp(envelope_value["computed_at"], "P5 risk-gate decision envelope.computed_at")
    if computed_at != expected_computed_at:
        raise ShadowRiskGateDecisionMismatchError("risk-gate decision envelope computed_at does not match P5 cycle")
    source_risk_control = _risk_control_reference(
        envelope_value["source_risk_control"], "P5 risk-gate decision envelope.source_risk_control"
    )
    expected_source = {
        "schema": FORWARD_OBSERVATION_RISK_CONTROL_SCHEMA,
        "risk_policy_id": expected_source_risk_control["risk_policy_id"],
        "risk_policy_version": expected_source_risk_control["risk_policy_version"],
        "risk_policy_sha256": expected_source_risk_control["risk_policy_sha256"],
    }
    if source_risk_control != expected_source:
        raise ShadowRiskGateDecisionMismatchError(
            "risk-gate decision envelope source_risk_control does not match shadow receipt risk_control"
        )
    envelope_sha256 = _digest(envelope_value["envelope_sha256"], "P5 risk-gate decision envelope.envelope_sha256")
    if envelope_sha256 != calculate_p5_risk_gate_decision_envelope_sha256(envelope_value):
        _fail("P5 risk-gate decision envelope.envelope_sha256 mismatch")
    decision = _validated_allow_decision(
        envelope_value["risk_gate_decision"], cycle_id=cycle_id, computed_at=computed_at
    )
    reference = {
        "schema": P5_RISK_GATE_DECISION_REFERENCE_SCHEMA,
        "source_risk_control": source_risk_control,
        "source_decision_schema": DETERMINISTIC_RISK_GATE_DECISION_SCHEMA,
        "evaluation_id": decision["evaluation_id"],
        "observed_at": decision["observed_at"],
        "risk_gate_policy": decision["policy"],
        "decision_sha256": decision["decision_sha256"],
    }
    return validate_p5_risk_gate_decision_reference(
        reference,
        expected_source_risk_control=expected_source_risk_control,
        expected_computed_at=expected_computed_at,
    )
