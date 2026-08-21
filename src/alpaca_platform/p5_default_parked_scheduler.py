"""One bounded, default-PARKED P5 shadow scheduling step.

The controller in :mod:`shadow_scheduler` validates and builds a virtual P5
receipt.  This module is deliberately a *thin orchestrator* around that
controller: it asks one injected, read-only artifact port for a bounded
snapshot, then (and only then) calls the existing create-only receipt-admission
port.  It has no filesystem, network, broker, account, credential, GitHub
Actions, or cron dependency.

It is intentionally safe to deploy before the external adapters exist.  A
missing reader, snapshot, risk envelope, policy receipt, previous receipt, or
store produces a sanitized ``PARKED`` status.  A valid policy receipt is still
validated by the existing P0 policy-gate path; this module neither signs nor
creates one, and it never resets a circuit breaker.
"""

from __future__ import annotations

import copy
import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Protocol

from .shadow_receipt_store import (
    CreateOnlyShadowReceiptStore,
    ShadowReceiptStoreError,
    persist_shadow_cycle_outcome,
    validate_shadow_receipt_persistence_result,
)
from .shadow_scheduler import (
    SCHEDULER_RESULT_SCHEMA,
    ShadowSchedulerError,
    run_tqqq_shadow_cycle,
    validate_shadow_scheduler_result,
)

P5_DEFAULT_PARKED_SCHEDULER_STATUS_SCHEMA = "qsl.tqqq_p5_default_parked_scheduler_status.v1"
P5_DEFAULT_PARKED_SCHEDULER_SUMMARY_SCHEMA = "qsl.tqqq_p5_default_parked_scheduler_summary.v1"

_DIGEST = re.compile(r"^[0-9a-f]{64}$")
_STATUS_FIELDS = {
    "schema",
    "cycle_id",
    "computed_at",
    "status",
    "reason_code",
    "shadow_receipt_sha256",
    "admission_sha256",
    "deduplication_sha256",
}
_SUMMARY_FIELDS = {"schema", "cycle_id", "status", "reason_code", "deduplication_sha256"}
_PARKED_REASONS = {
    "artifact_reader_missing",
    "artifact_reader_invalid",
    "artifact_snapshot_missing",
    "artifact_snapshot_unavailable",
    "artifact_snapshot_invalid",
    "receipt_store_missing",
    "receipt_store_invalid",
    "forward_observation_missing",
    "policy_gate_receipt_missing",
    "risk_control_missing",
    "deployment_bundle_missing",
    "prior_receipt_invalid",
    "cycle_input_invalid",
    "ledger_receipt_invalid",
    "risk_gate_decision_missing",
    "risk_gate_decision_invalid",
    "risk_gate_decision_mismatch",
    "risk_gate_decision_prohibited",
    "receipt_conflict",
}


class P5DefaultParkedSchedulerError(ValueError):
    """Raised when a scheduler request or a status artifact is malformed."""


class RestrictedP5ArtifactReadError(RuntimeError):
    """A bounded reader uses this for an unavailable snapshot, without details."""


@dataclass(frozen=True)
class P5ShadowArtifactSnapshot:
    """The only artifact material a single P5 scheduling step may consume.

    Values are opaque to the reader boundary.  The downstream P5 controller and
    receipt-admission adapter re-validate every one of them before a receipt
    store is touched.  The snapshot intentionally has no account, order,
    credential, endpoint, or market-data field.
    """

    cycle_id: str
    forward_observation: Any | None
    policy_gate_receipt: Any | None
    risk_control: Any | None
    deployment_bundle_sha256: str | None
    risk_gate_decision: Any | None
    prior_receipt: Any | None = None


class RestrictedP5ShadowArtifactReader(Protocol):
    """Read one already-local, bounded snapshot for exactly one P5 cycle."""

    def read_snapshot(self, *, cycle_id: str) -> P5ShadowArtifactSnapshot | None:
        """Return a snapshot once, ``None`` when it is not available yet."""


@dataclass(frozen=True)
class P5DefaultParkedSchedulerOutcome:
    """The sanitized status plus a compact immutable de-duplication summary."""

    status: dict[str, Any]
    summary: dict[str, Any]


def _fail(message: str) -> None:
    raise P5DefaultParkedSchedulerError(message)


def _exact_keys(value: Mapping[str, Any], expected: set[str], label: str) -> None:
    missing = sorted(expected - set(value))
    unknown = sorted(set(value) - expected)
    if missing:
        _fail(f"{label} missing required field(s): {', '.join(missing)}")
    if unknown:
        _fail(f"{label} has unknown field(s): {', '.join(unknown)}")


def _identity_and_time(cycle_id: Any, computed_at: Any) -> tuple[str, str]:
    """Use the existing controller's public identity/time validator."""
    try:
        normalized = validate_shadow_scheduler_result(
            {
                "schema": SCHEDULER_RESULT_SCHEMA,
                "cycle_id": cycle_id,
                "computed_at": computed_at,
                "status": "PARKED",
                "reason_code": "forward_observation_missing",
                "shadow_receipt_sha256": None,
            }
        )
    except ShadowSchedulerError as exc:
        raise P5DefaultParkedSchedulerError("P5 scheduler cycle identity is invalid") from exc
    return normalized["cycle_id"], normalized["computed_at"]


def _digest(value: Any, label: str) -> str:
    if not isinstance(value, str) or not _DIGEST.fullmatch(value):
        _fail(f"{label} must be a lowercase SHA-256 digest")
    return value


def _canonical_json(value: Mapping[str, Any], excluded_field: str) -> str:
    content = dict(value)
    content.pop(excluded_field, None)
    try:
        return json.dumps(content, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise P5DefaultParkedSchedulerError("P5 scheduler status cannot be represented as canonical JSON") from exc


def calculate_p5_default_parked_scheduler_deduplication_sha256(value: Mapping[str, Any]) -> str:
    """Bind the sanitized status to one repeatable scheduler outcome.

    The digest intentionally covers only cycle identity, time, status/reason,
    and receipt/admission digests.  It never includes artifact payloads.
    """
    return hashlib.sha256(_canonical_json(value, "deduplication_sha256").encode("utf-8")).hexdigest()


def validate_p5_default_parked_scheduler_status(value: Any) -> dict[str, Any]:
    """Validate a safe-to-publish P5 default-PARKED scheduler status."""
    if not isinstance(value, Mapping):
        _fail("P5 scheduler status must be an object")
    _exact_keys(value, _STATUS_FIELDS, "P5 scheduler status")
    if value["schema"] != P5_DEFAULT_PARKED_SCHEDULER_STATUS_SCHEMA:
        _fail(f"P5 scheduler status.schema must be {P5_DEFAULT_PARKED_SCHEDULER_STATUS_SCHEMA}")
    cycle_id, computed_at = _identity_and_time(value["cycle_id"], value["computed_at"])
    status = value["status"]
    reason_code = value["reason_code"]
    receipt_sha256 = value["shadow_receipt_sha256"]
    admission_sha256 = value["admission_sha256"]
    if status == "PARKED":
        if reason_code not in _PARKED_REASONS or receipt_sha256 is not None or admission_sha256 is not None:
            _fail("PARKED scheduler status must have a known reason and no receipt/admission digests")
    elif status in {"RECORDED", "RECONCILED"}:
        expected_reason = "receipt_recorded" if status == "RECORDED" else "receipt_reconciled"
        if reason_code != expected_reason:
            _fail("scheduler status reason_code does not match its recorded/reconciled state")
        receipt_sha256 = _digest(receipt_sha256, "P5 scheduler status.shadow_receipt_sha256")
        admission_sha256 = _digest(admission_sha256, "P5 scheduler status.admission_sha256")
    else:
        _fail("P5 scheduler status.status must be PARKED, RECORDED, or RECONCILED")
    normalized = {
        "schema": P5_DEFAULT_PARKED_SCHEDULER_STATUS_SCHEMA,
        "cycle_id": cycle_id,
        "computed_at": computed_at,
        "status": status,
        "reason_code": reason_code,
        "shadow_receipt_sha256": receipt_sha256,
        "admission_sha256": admission_sha256,
        "deduplication_sha256": _digest(
            value["deduplication_sha256"], "P5 scheduler status.deduplication_sha256"
        ),
    }
    if normalized["deduplication_sha256"] != calculate_p5_default_parked_scheduler_deduplication_sha256(normalized):
        _fail("P5 scheduler status.deduplication_sha256 mismatch")
    return normalized


def _status(
    *,
    cycle_id: str,
    computed_at: str,
    status: str,
    reason_code: str,
    shadow_receipt_sha256: str | None,
    admission_sha256: str | None,
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "schema": P5_DEFAULT_PARKED_SCHEDULER_STATUS_SCHEMA,
        "cycle_id": cycle_id,
        "computed_at": computed_at,
        "status": status,
        "reason_code": reason_code,
        "shadow_receipt_sha256": shadow_receipt_sha256,
        "admission_sha256": admission_sha256,
        "deduplication_sha256": "",
    }
    result["deduplication_sha256"] = calculate_p5_default_parked_scheduler_deduplication_sha256(result)
    return validate_p5_default_parked_scheduler_status(result)


def summarize_p5_default_parked_scheduler_status(status: Any) -> dict[str, Any]:
    """Return the small status/de-duplication interface for an outer runner."""
    normalized = validate_p5_default_parked_scheduler_status(status)
    return {
        "schema": P5_DEFAULT_PARKED_SCHEDULER_SUMMARY_SCHEMA,
        "cycle_id": normalized["cycle_id"],
        "status": normalized["status"],
        "reason_code": normalized["reason_code"],
        "deduplication_sha256": normalized["deduplication_sha256"],
    }


def validate_p5_default_parked_scheduler_summary(value: Any) -> dict[str, Any]:
    """Validate the compact, non-sensitive outer-runner summary."""
    if not isinstance(value, Mapping):
        _fail("P5 scheduler summary must be an object")
    _exact_keys(value, _SUMMARY_FIELDS, "P5 scheduler summary")
    if value["schema"] != P5_DEFAULT_PARKED_SCHEDULER_SUMMARY_SCHEMA:
        _fail(f"P5 scheduler summary.schema must be {P5_DEFAULT_PARKED_SCHEDULER_SUMMARY_SCHEMA}")
    cycle_id, _ = _identity_and_time(value["cycle_id"], "2026-01-01T00:00:00Z")
    if value["status"] not in {"PARKED", "RECORDED", "RECONCILED"}:
        _fail("P5 scheduler summary.status must be PARKED, RECORDED, or RECONCILED")
    if not isinstance(value["reason_code"], str):
        _fail("P5 scheduler summary.reason_code must be a string")
    return {
        "schema": P5_DEFAULT_PARKED_SCHEDULER_SUMMARY_SCHEMA,
        "cycle_id": cycle_id,
        "status": value["status"],
        "reason_code": value["reason_code"],
        "deduplication_sha256": _digest(
            value["deduplication_sha256"], "P5 scheduler summary.deduplication_sha256"
        ),
    }


def _outcome(status: dict[str, Any]) -> P5DefaultParkedSchedulerOutcome:
    normalized = validate_p5_default_parked_scheduler_status(status)
    return P5DefaultParkedSchedulerOutcome(
        status=normalized,
        summary=validate_p5_default_parked_scheduler_summary(
            summarize_p5_default_parked_scheduler_status(normalized)
        ),
    )


def _park(*, cycle_id: str, computed_at: str, reason_code: str) -> P5DefaultParkedSchedulerOutcome:
    return _outcome(
        _status(
            cycle_id=cycle_id,
            computed_at=computed_at,
            status="PARKED",
            reason_code=reason_code,
            shadow_receipt_sha256=None,
            admission_sha256=None,
        )
    )


class InMemoryRestrictedP5ShadowArtifactReader:
    """A read-only-in-use, in-memory snapshot double for tests/local replay.

    It cannot discover files, invoke a network client, or retain a caller's
    mutable mapping by reference.  ``put_snapshot`` is setup-only; production
    adapters must implement :class:`RestrictedP5ShadowArtifactReader` instead.
    """

    def __init__(self) -> None:
        self._snapshots: dict[str, P5ShadowArtifactSnapshot] = {}
        self.read_count = 0

    def put_snapshot(self, snapshot: P5ShadowArtifactSnapshot) -> None:
        if not isinstance(snapshot, P5ShadowArtifactSnapshot):
            raise P5DefaultParkedSchedulerError("in-memory P5 snapshot must have the bounded snapshot type")
        cycle_id, _ = _identity_and_time(snapshot.cycle_id, "2026-01-01T00:00:00Z")
        self._snapshots[cycle_id] = copy.deepcopy(snapshot)

    def read_snapshot(self, *, cycle_id: str) -> P5ShadowArtifactSnapshot | None:
        self.read_count += 1
        snapshot = self._snapshots.get(cycle_id)
        return copy.deepcopy(snapshot) if snapshot is not None else None


def run_p5_default_parked_shadow_cycle(
    *,
    cycle_id: str,
    computed_at: str,
    artifact_reader: RestrictedP5ShadowArtifactReader | None = None,
    receipt_store: CreateOnlyShadowReceiptStore | None = None,
) -> P5DefaultParkedSchedulerOutcome:
    """Run one non-self-scheduling P5 cycle through bounded injected ports.

    This function neither installs a timer nor performs retries.  It reads one
    snapshot at most once.  Missing/invalid source artifacts are represented as
    ``PARKED`` before the receipt store is read or written.  Only an existing
    P0 policy-gate receipt, a closed deterministic risk envelope, and all
    controller prerequisites can reach ``persist_shadow_cycle_outcome``.
    """
    normalized_cycle_id, normalized_computed_at = _identity_and_time(cycle_id, computed_at)
    if artifact_reader is None:
        return _park(
            cycle_id=normalized_cycle_id,
            computed_at=normalized_computed_at,
            reason_code="artifact_reader_missing",
        )
    if not callable(getattr(artifact_reader, "read_snapshot", None)):
        return _park(
            cycle_id=normalized_cycle_id,
            computed_at=normalized_computed_at,
            reason_code="artifact_reader_invalid",
        )
    try:
        snapshot = artifact_reader.read_snapshot(cycle_id=normalized_cycle_id)
    except RestrictedP5ArtifactReadError:
        return _park(
            cycle_id=normalized_cycle_id,
            computed_at=normalized_computed_at,
            reason_code="artifact_snapshot_unavailable",
        )
    if snapshot is None:
        return _park(
            cycle_id=normalized_cycle_id,
            computed_at=normalized_computed_at,
            reason_code="artifact_snapshot_missing",
        )
    if not isinstance(snapshot, P5ShadowArtifactSnapshot) or snapshot.cycle_id != normalized_cycle_id:
        return _park(
            cycle_id=normalized_cycle_id,
            computed_at=normalized_computed_at,
            reason_code="artifact_snapshot_invalid",
        )
    if receipt_store is None:
        return _park(
            cycle_id=normalized_cycle_id,
            computed_at=normalized_computed_at,
            reason_code="receipt_store_missing",
        )
    if not callable(getattr(receipt_store, "read", None)) or not callable(
        getattr(receipt_store, "create_if_absent", None)
    ):
        return _park(
            cycle_id=normalized_cycle_id,
            computed_at=normalized_computed_at,
            reason_code="receipt_store_invalid",
        )

    controller_outcome = run_tqqq_shadow_cycle(
        cycle_id=normalized_cycle_id,
        computed_at=normalized_computed_at,
        forward_observation=snapshot.forward_observation,
        policy_gate_receipt=snapshot.policy_gate_receipt,
        risk_control=snapshot.risk_control,
        deployment_bundle_sha256=snapshot.deployment_bundle_sha256,
        prior_receipt=snapshot.prior_receipt,
    )
    try:
        persistence = validate_shadow_receipt_persistence_result(
            persist_shadow_cycle_outcome(
                controller_outcome,
                receipt_store,
                risk_gate_decision=snapshot.risk_gate_decision,
            )
        )
    except ShadowReceiptStoreError as exc:
        raise P5DefaultParkedSchedulerError("P5 receipt store contract is invalid") from exc
    return _outcome(
        _status(
            cycle_id=persistence["cycle_id"],
            computed_at=persistence["computed_at"],
            status=persistence["status"],
            reason_code=persistence["reason_code"],
            shadow_receipt_sha256=persistence["shadow_receipt_sha256"],
            admission_sha256=persistence["admission_sha256"],
        )
    )
