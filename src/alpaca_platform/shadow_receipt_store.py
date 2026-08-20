"""Create-only, no-broker persistence seam for bounded P5 shadow receipts.

This module runs only after ``shadow_scheduler``.  It wraps an already
validated ``RECEIPT_READY`` result in a closed admission artifact and exposes a
small create-only storage port.  The included in-memory implementation is for
tests/local deterministic replay only: it has no filesystem, network,
credential, market-data, broker, or scheduler dependency.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from typing import Any, Protocol

from .shadow_ledger import ShadowLedgerError, validate_shadow_ledger_receipt
from .shadow_scheduler import (
    ShadowCycleOutcome,
    ShadowSchedulerError,
    validate_shadow_scheduler_result,
)

ADMISSION_SCHEMA = "qsl.tqqq_shadow_receipt_admission.v1"
PERSISTENCE_RESULT_SCHEMA = "qsl.tqqq_shadow_receipt_persistence_result.v1"

_DIGEST = re.compile(r"^[0-9a-f]{64}$")
_ADMISSION_FIELDS = {
    "schema",
    "cycle_id",
    "computed_at",
    "scheduler_result",
    "shadow_receipt",
    "admission_sha256",
}
_PERSISTENCE_RESULT_FIELDS = {
    "schema",
    "cycle_id",
    "computed_at",
    "status",
    "reason_code",
    "shadow_receipt_sha256",
    "admission_sha256",
}
_PARKED_REASONS = {
    "forward_observation_missing",
    "policy_gate_receipt_missing",
    "risk_control_missing",
    "deployment_bundle_missing",
    "prior_receipt_invalid",
    "cycle_input_invalid",
    "ledger_receipt_invalid",
    "receipt_conflict",
}


class ShadowReceiptStoreError(ValueError):
    """Raised when a persistence-admission contract is invalid or ambiguous."""


class CreateOnlyShadowReceiptStore(Protocol):
    """Minimal production seam for immutable P5 receipt persistence.

    ``create_if_absent`` must be atomic for a ``cycle_id``.  A false return
    means that an admission already exists and must be read/compared before it
    is treated as reconciled.
    """

    def read(self, cycle_id: str) -> Mapping[str, Any] | None:
        """Return the stored admission for exactly one cycle, if any."""

    def create_if_absent(self, admission: Mapping[str, Any]) -> bool:
        """Store an admission once, returning false without overwriting it."""


def _fail(message: str) -> None:
    raise ShadowReceiptStoreError(message)


def _canonical_json(value: Mapping[str, Any], excluded_field: str, label: str) -> str:
    content = dict(value)
    content.pop(excluded_field, None)
    try:
        return json.dumps(content, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise ShadowReceiptStoreError(f"{label} cannot be represented as canonical JSON") from exc


def _clone(value: Mapping[str, Any], label: str) -> dict[str, Any]:
    try:
        return json.loads(json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False))
    except (TypeError, ValueError) as exc:
        raise ShadowReceiptStoreError(f"{label} cannot be copied as JSON") from exc


def _exact_keys(value: Mapping[str, Any], expected: set[str], label: str) -> None:
    missing = sorted(expected - set(value))
    unknown = sorted(set(value) - expected)
    if missing:
        _fail(f"{label} missing required field(s): {', '.join(missing)}")
    if unknown:
        _fail(f"{label} has unknown field(s): {', '.join(unknown)}")


def _digest(value: Any, label: str) -> str:
    if not isinstance(value, str) or not _DIGEST.fullmatch(value):
        _fail(f"{label} must be a lowercase SHA-256 digest")
    return value


def calculate_shadow_receipt_admission_sha256(value: Mapping[str, Any]) -> str:
    """Return the stable digest for one closed persistence-admission artifact."""
    return hashlib.sha256(
        _canonical_json(value, "admission_sha256", "shadow receipt admission").encode("utf-8")
    ).hexdigest()


def validate_shadow_receipt_admission(value: Any) -> dict[str, Any]:
    """Validate a non-secret, immutable P5 receipt admission artifact."""
    if not isinstance(value, Mapping):
        _fail("shadow receipt admission must be an object")
    _exact_keys(value, _ADMISSION_FIELDS, "shadow receipt admission")
    if value["schema"] != ADMISSION_SCHEMA:
        _fail(f"shadow receipt admission.schema must be {ADMISSION_SCHEMA}")
    try:
        result = validate_shadow_scheduler_result(value["scheduler_result"])
    except ShadowSchedulerError as exc:
        raise ShadowReceiptStoreError("shadow receipt admission scheduler result is invalid") from exc
    if result["status"] != "RECEIPT_READY":
        _fail("shadow receipt admission scheduler result must be RECEIPT_READY")
    try:
        receipt = validate_shadow_ledger_receipt(value["shadow_receipt"])
    except ShadowLedgerError as exc:
        raise ShadowReceiptStoreError("shadow receipt admission ledger receipt is invalid") from exc
    if value["cycle_id"] != result["cycle_id"] or value["cycle_id"] != receipt["cycle_id"]:
        _fail("shadow receipt admission cycle_id must match controller result and ledger receipt")
    if value["computed_at"] != result["computed_at"]:
        _fail("shadow receipt admission computed_at must match controller result")
    if result["shadow_receipt_sha256"] != receipt["receipt_sha256"]:
        _fail("shadow receipt admission scheduler result must bind ledger receipt digest")
    normalized = {
        "schema": ADMISSION_SCHEMA,
        "cycle_id": result["cycle_id"],
        "computed_at": result["computed_at"],
        "scheduler_result": result,
        "shadow_receipt": receipt,
        "admission_sha256": _digest(value["admission_sha256"], "shadow receipt admission.admission_sha256"),
    }
    if normalized["admission_sha256"] != calculate_shadow_receipt_admission_sha256(normalized):
        _fail("shadow receipt admission.admission_sha256 mismatch")
    return normalized


def build_shadow_receipt_admission(outcome: ShadowCycleOutcome) -> dict[str, Any]:
    """Convert one controller ``RECEIPT_READY`` outcome into an admission.

    This function cannot create a ledger receipt.  A parked result, a missing
    receipt, or an inconsistent outcome is rejected before a store is called.
    """
    try:
        result = validate_shadow_scheduler_result(outcome.result)
    except (AttributeError, ShadowSchedulerError) as exc:
        raise ShadowReceiptStoreError("shadow cycle outcome result is invalid") from exc
    if result["status"] != "RECEIPT_READY":
        _fail("only a RECEIPT_READY controller outcome can be admitted")
    if outcome.receipt is None:
        _fail("RECEIPT_READY controller outcome must include a ledger receipt")
    admission: dict[str, Any] = {
        "schema": ADMISSION_SCHEMA,
        "cycle_id": result["cycle_id"],
        "computed_at": result["computed_at"],
        "scheduler_result": result,
        "shadow_receipt": outcome.receipt,
        "admission_sha256": "",
    }
    admission["admission_sha256"] = calculate_shadow_receipt_admission_sha256(admission)
    return validate_shadow_receipt_admission(admission)


def _persistence_result(
    *,
    cycle_id: str,
    computed_at: str,
    status: str,
    reason_code: str,
    shadow_receipt_sha256: str | None,
    admission_sha256: str | None,
) -> dict[str, Any]:
    return validate_shadow_receipt_persistence_result(
        {
            "schema": PERSISTENCE_RESULT_SCHEMA,
            "cycle_id": cycle_id,
            "computed_at": computed_at,
            "status": status,
            "reason_code": reason_code,
            "shadow_receipt_sha256": shadow_receipt_sha256,
            "admission_sha256": admission_sha256,
        }
    )


def validate_shadow_receipt_persistence_result(value: Any) -> dict[str, Any]:
    """Validate the small, sanitized result a future scheduler may publish."""
    if not isinstance(value, Mapping):
        _fail("shadow receipt persistence result must be an object")
    _exact_keys(value, _PERSISTENCE_RESULT_FIELDS, "shadow receipt persistence result")
    if value["schema"] != PERSISTENCE_RESULT_SCHEMA:
        _fail(f"shadow receipt persistence result.schema must be {PERSISTENCE_RESULT_SCHEMA}")
    try:
        identity = validate_shadow_scheduler_result(
            {
                "schema": "qsl.tqqq_shadow_scheduler_result.v1",
                "cycle_id": value["cycle_id"],
                "computed_at": value["computed_at"],
                "status": "PARKED",
                "reason_code": "forward_observation_missing",
                "shadow_receipt_sha256": None,
            }
        )
    except ShadowSchedulerError as exc:
        raise ShadowReceiptStoreError("shadow receipt persistence result identity is invalid") from exc

    status = value["status"]
    reason_code = value["reason_code"]
    receipt_sha256 = value["shadow_receipt_sha256"]
    admission_sha256 = value["admission_sha256"]
    if status == "PARKED":
        if reason_code not in _PARKED_REASONS or receipt_sha256 is not None or admission_sha256 is not None:
            _fail("PARKED persistence result must have a known reason and no digests")
    elif status in {"RECORDED", "RECONCILED"}:
        expected_reason = "receipt_recorded" if status == "RECORDED" else "receipt_reconciled"
        if reason_code != expected_reason:
            _fail("persistence result reason_code does not match status")
        receipt_sha256 = _digest(receipt_sha256, "shadow receipt persistence result.shadow_receipt_sha256")
        admission_sha256 = _digest(admission_sha256, "shadow receipt persistence result.admission_sha256")
    else:
        _fail("shadow receipt persistence result.status must be PARKED, RECORDED, or RECONCILED")
    return {
        "schema": PERSISTENCE_RESULT_SCHEMA,
        "cycle_id": identity["cycle_id"],
        "computed_at": identity["computed_at"],
        "status": status,
        "reason_code": reason_code,
        "shadow_receipt_sha256": receipt_sha256,
        "admission_sha256": admission_sha256,
    }


class InMemoryShadowReceiptStore:
    """Small create-only storage double for deterministic tests and local replay."""

    def __init__(self) -> None:
        self._admissions: dict[str, dict[str, Any]] = {}

    def read(self, cycle_id: str) -> dict[str, Any] | None:
        stored = self._admissions.get(cycle_id)
        return _clone(stored, "stored shadow receipt admission") if stored is not None else None

    def create_if_absent(self, admission: Mapping[str, Any]) -> bool:
        normalized = validate_shadow_receipt_admission(admission)
        cycle_id = normalized["cycle_id"]
        if cycle_id in self._admissions:
            return False
        self._admissions[cycle_id] = _clone(normalized, "shadow receipt admission")
        return True


def persist_shadow_cycle_outcome(
    outcome: ShadowCycleOutcome,
    store: CreateOnlyShadowReceiptStore,
) -> dict[str, Any]:
    """Persist a ready P5 receipt once, or return a closed parked/reconciled result.

    It never promotes P5, retries missing inputs, or writes a parked result. A
    conflicting immutable cycle returns ``PARKED`` and remains untouched.
    """
    try:
        controller_result = validate_shadow_scheduler_result(outcome.result)
    except (AttributeError, ShadowSchedulerError) as exc:
        raise ShadowReceiptStoreError("shadow cycle outcome result is invalid") from exc
    if controller_result["status"] == "PARKED":
        if outcome.receipt is not None:
            _fail("PARKED controller outcome must not include a ledger receipt")
        return _persistence_result(
            cycle_id=controller_result["cycle_id"],
            computed_at=controller_result["computed_at"],
            status="PARKED",
            reason_code=controller_result["reason_code"],
            shadow_receipt_sha256=None,
            admission_sha256=None,
        )

    admission = build_shadow_receipt_admission(outcome)
    cycle_id = admission["cycle_id"]
    existing = store.read(cycle_id)
    created = existing is None and store.create_if_absent(admission)

    stored = store.read(cycle_id)
    if stored is None:
        _fail("create-only shadow receipt store did not retain or expose the cycle admission")
    stored_admission = validate_shadow_receipt_admission(stored)
    if stored_admission["admission_sha256"] == admission["admission_sha256"]:
        return _persistence_result(
            cycle_id=cycle_id,
            computed_at=admission["computed_at"],
            status="RECORDED" if created else "RECONCILED",
            reason_code="receipt_recorded" if created else "receipt_reconciled",
            shadow_receipt_sha256=admission["shadow_receipt"]["receipt_sha256"],
            admission_sha256=admission["admission_sha256"],
        )
    return _persistence_result(
        cycle_id=cycle_id,
        computed_at=admission["computed_at"],
        status="PARKED",
        reason_code="receipt_conflict",
        shadow_receipt_sha256=None,
        admission_sha256=None,
    )
