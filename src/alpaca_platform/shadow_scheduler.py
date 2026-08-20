"""Bounded, no-broker control step for one TQQQ P5 shadow cycle.

This module deliberately does not schedule itself, fetch upstream artifacts, or
write a ledger.  A future scheduler supplies already-local artifacts and can
persist the returned receipt through a separate create-only adapter.  Missing
or invalid prerequisites produce a small, stable PARKED result instead of a
retry/error loop or an implied authorization.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from .shadow_ledger import (
    ShadowLedgerError,
    build_shadow_ledger_receipt,
    build_tqqq_shadow_cycle_input,
    validate_shadow_ledger_receipt,
)

SCHEDULER_RESULT_SCHEMA = "qsl.tqqq_shadow_scheduler_result.v1"

_IDENTITY = re.compile(r"^[a-z][a-z0-9]*(?:[._-][a-z0-9]+)*$")
_TIMESTAMP = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z$")
_DIGEST = re.compile(r"^[0-9a-f]{64}$")
_RESULT_FIELDS = {
    "schema",
    "cycle_id",
    "computed_at",
    "status",
    "reason_code",
    "shadow_receipt_sha256",
}
_PARKED_REASONS = {
    "forward_observation_missing",
    "policy_gate_receipt_missing",
    "risk_control_missing",
    "deployment_bundle_missing",
    "prior_receipt_invalid",
    "cycle_input_invalid",
    "ledger_receipt_invalid",
}


class ShadowSchedulerError(ValueError):
    """Raised when the scheduler request/result shape is invalid."""


@dataclass(frozen=True)
class ShadowCycleOutcome:
    """A safe control-plane result plus an optional unpersisted ledger receipt."""

    result: dict[str, Any]
    receipt: dict[str, Any] | None


def _identity(value: Any, label: str) -> str:
    if not isinstance(value, str) or not _IDENTITY.fullmatch(value):
        raise ShadowSchedulerError(f"{label} must be a lowercase immutable identity")
    return value


def _timestamp(value: Any, label: str) -> str:
    if not isinstance(value, str) or not _TIMESTAMP.fullmatch(value):
        raise ShadowSchedulerError(f"{label} must be an RFC3339 UTC timestamp with whole seconds")
    try:
        datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
    except ValueError as exc:
        raise ShadowSchedulerError(f"{label} must be a valid calendar timestamp") from exc
    return value


def validate_shadow_scheduler_result(value: Any) -> dict[str, Any]:
    """Validate the small status artifact a scheduler may publish externally."""
    if not isinstance(value, dict):
        raise ShadowSchedulerError("shadow scheduler result must be an object")
    missing = sorted(_RESULT_FIELDS - set(value))
    unknown = sorted(set(value) - _RESULT_FIELDS)
    if missing:
        raise ShadowSchedulerError(f"shadow scheduler result missing required field(s): {', '.join(missing)}")
    if unknown:
        raise ShadowSchedulerError(f"shadow scheduler result has unknown field(s): {', '.join(unknown)}")
    if value["schema"] != SCHEDULER_RESULT_SCHEMA:
        raise ShadowSchedulerError(f"shadow scheduler result.schema must be {SCHEDULER_RESULT_SCHEMA}")

    status = value["status"]
    reason_code = value["reason_code"]
    receipt_sha256 = value["shadow_receipt_sha256"]
    if status == "PARKED":
        if reason_code not in _PARKED_REASONS or receipt_sha256 is not None:
            raise ShadowSchedulerError("PARKED result must have a known reason and no receipt digest")
    elif status == "RECEIPT_READY":
        if (
            reason_code != "receipt_ready"
            or not isinstance(receipt_sha256, str)
            or not _DIGEST.fullmatch(receipt_sha256)
        ):
            raise ShadowSchedulerError("RECEIPT_READY result must contain its receipt digest")
    else:
        raise ShadowSchedulerError("shadow scheduler result.status must be PARKED or RECEIPT_READY")
    return {
        "schema": SCHEDULER_RESULT_SCHEMA,
        "cycle_id": _identity(value["cycle_id"], "shadow scheduler result.cycle_id"),
        "computed_at": _timestamp(value["computed_at"], "shadow scheduler result.computed_at"),
        "status": status,
        "reason_code": reason_code,
        "shadow_receipt_sha256": receipt_sha256,
    }


def _park(*, cycle_id: str, computed_at: str, reason_code: str) -> ShadowCycleOutcome:
    result = validate_shadow_scheduler_result(
        {
            "schema": SCHEDULER_RESULT_SCHEMA,
            "cycle_id": cycle_id,
            "computed_at": computed_at,
            "status": "PARKED",
            "reason_code": reason_code,
            "shadow_receipt_sha256": None,
        }
    )
    return ShadowCycleOutcome(result=result, receipt=None)


def run_tqqq_shadow_cycle(
    *,
    cycle_id: str,
    computed_at: str,
    forward_observation: Any | None,
    policy_gate_receipt: Any | None,
    risk_control: Any | None,
    deployment_bundle_sha256: str | None,
    prior_receipt: Any | None = None,
) -> ShadowCycleOutcome:
    """Prepare one P5 receipt only when every bounded prerequisite is present.

    Expected data absence and invalid external artifacts are intentionally
    represented as a closed, sanitized ``PARKED`` result.  This is a pure
    control step: it neither persists the returned receipt nor accesses a
    broker, credential, network endpoint, or market-data source.
    """
    normalized_cycle_id = _identity(cycle_id, "shadow scheduler cycle_id")
    normalized_computed_at = _timestamp(computed_at, "shadow scheduler computed_at")

    if forward_observation is None:
        return _park(
            cycle_id=normalized_cycle_id,
            computed_at=normalized_computed_at,
            reason_code="forward_observation_missing",
        )
    if policy_gate_receipt is None:
        return _park(
            cycle_id=normalized_cycle_id,
            computed_at=normalized_computed_at,
            reason_code="policy_gate_receipt_missing",
        )
    if risk_control is None:
        return _park(
            cycle_id=normalized_cycle_id,
            computed_at=normalized_computed_at,
            reason_code="risk_control_missing",
        )
    if deployment_bundle_sha256 is None:
        return _park(
            cycle_id=normalized_cycle_id,
            computed_at=normalized_computed_at,
            reason_code="deployment_bundle_missing",
        )

    if prior_receipt is not None:
        try:
            validate_shadow_ledger_receipt(prior_receipt)
        except ShadowLedgerError:
            return _park(
                cycle_id=normalized_cycle_id,
                computed_at=normalized_computed_at,
                reason_code="prior_receipt_invalid",
            )

    try:
        cycle_input = build_tqqq_shadow_cycle_input(
            forward_observation=forward_observation,
            policy_gate_receipt=policy_gate_receipt,
            risk_control=risk_control,
            deployment_bundle_sha256=deployment_bundle_sha256,
            cycle_id=normalized_cycle_id,
            produced_at=normalized_computed_at,
        )
    except ShadowLedgerError:
        return _park(
            cycle_id=normalized_cycle_id,
            computed_at=normalized_computed_at,
            reason_code="cycle_input_invalid",
        )

    try:
        receipt = build_shadow_ledger_receipt(cycle_input, prior_receipt=prior_receipt)
    except ShadowLedgerError:
        return _park(
            cycle_id=normalized_cycle_id,
            computed_at=normalized_computed_at,
            reason_code="ledger_receipt_invalid",
        )

    result = validate_shadow_scheduler_result(
        {
            "schema": SCHEDULER_RESULT_SCHEMA,
            "cycle_id": normalized_cycle_id,
            "computed_at": normalized_computed_at,
            "status": "RECEIPT_READY",
            "reason_code": "receipt_ready",
            "shadow_receipt_sha256": receipt["receipt_sha256"],
        }
    )
    return ShadowCycleOutcome(result=result, receipt=receipt)
