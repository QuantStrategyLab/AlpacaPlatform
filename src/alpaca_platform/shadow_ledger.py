"""Pure, no-broker P5 shadow-ledger receipt construction for frozen TQQQ input.

There is intentionally no Alpaca client in this module. The caller supplies a
validated forward decision and a bounded receipt from the independently
protected policy gate, then writes the resulting receipt to its own create-only
storage. The module neither reads credentials nor makes network requests nor
produces broker order payloads.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import sys
from collections.abc import Mapping
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

INPUT_SCHEMA = "qsl.tqqq_shadow_cycle_input.v2"
RECEIPT_SCHEMA = "qsl.tqqq_shadow_ledger_receipt.v2"
POLICY_GATE_RECEIPT_SCHEMA = "qsl.gcp_kms_policy_gate_receipt.v1"
CANDIDATE_ID = "tqqq_core_only_p2_v5"
_DIGEST = re.compile(r"^[0-9a-f]{64}$")
_REVISION = re.compile(r"^[0-9a-f]{40}$")
_IDENTITY = re.compile(r"^[a-z][a-z0-9]*(?:[._-][a-z0-9]+)*$")
_TIMESTAMP = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z$")
_ALLOCATION_SYMBOLS = ("TQQQ", "QQQM", "BOXX", "CASH")
_GENESIS_DIGEST = "0" * 64
_FORBIDDEN_KEY = re.compile(
    r"credential|secret|token|password|cookie|jwt|private(?:[_-]?key)?|access[_-]?key|"
    r"broker|order|account|position|balance|fill|notional|price|capital|endpoint|url",
    re.IGNORECASE,
)
_URL = re.compile(r"[a-z][a-z0-9+.-]*://", re.IGNORECASE)
_INPUT_FIELDS = {
    "schema",
    "cycle_id",
    "produced_at",
    "deployment_bundle_sha256",
    "candidate",
    "source_evidence",
    "forward_decision",
    "risk_control",
    "policy_gate_receipt",
    "input_sha256",
}
_CANDIDATE_FIELDS = {"candidate_id", "config_sha256", "strategy_repository", "strategy_revision"}
_EVIDENCE_FIELDS = {"p1_manifest_sha256", "p2_config_sha256", "p3_evidence_sha256", "producer_revision"}
_DECISION_FIELDS = {"decision_id", "effective_session", "producer_revision", "allocation_bps", "decision_sha256"}
_RISK_CONTROL_FIELDS = {"stage", "execution_lane", "risk_policy_id", "risk_policy_version", "risk_policy_sha256"}
_POLICY_GATE_RECEIPT_FIELDS = {
    "schema",
    "verified_at",
    "deployment_bundle",
    "policy",
    "activation",
    "target",
    "risk_control",
    "trusted_policy_root",
    "signature_sha256",
    "receipt_sha256",
}
_POLICY_GATE_BUNDLE_FIELDS = {"schema", "bundle_id", "bundle_sha256"}
_POLICY_GATE_POLICY_FIELDS = {
    "policy_id",
    "policy_version",
    "policy_sha256",
    "stage",
    "effective_at",
    "expires_at",
}
_POLICY_GATE_ACTIVATION_FIELDS = {"activation_id", "activation_sha256", "effective_at", "expires_at"}
_POLICY_GATE_TARGET_FIELDS = {"platform", "repository", "revision", "environment", "target_sha256"}
_POLICY_GATE_RISK_FIELDS = {"risk_policy_id", "risk_policy_version", "risk_policy_sha256"}
_POLICY_GATE_ROOT_FIELDS = {"root_id", "trusted_policy_root_sha256", "expires_at"}
_RECEIPT_FIELDS = {
    "schema", "cycle_id", "produced_at", "deployment_bundle_sha256", "candidate", "source_evidence", "forward_decision", "risk_control",
    "policy_gate_receipt",
    "ledger_parent_sha256", "shadow_adjustments_bps", "receipt_sha256",
}


class ShadowLedgerError(ValueError):
    """Raised when a shadow receipt would be invalid, ambiguous, or unsafe."""


def _fail(message: str) -> None:
    raise ShadowLedgerError(message)


def _object(value: Any, path: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        _fail(f"{path} must be an object")
    return value


def _exact_keys(value: Mapping[str, Any], expected: set[str], path: str) -> None:
    missing = sorted(expected - set(value))
    unknown = sorted(set(value) - expected)
    if missing:
        _fail(f"{path} missing required field(s): {', '.join(missing)}")
    if unknown:
        _fail(f"{path} has unknown field(s): {', '.join(unknown)}")


def _reject_unsafe_material(value: Any, path: str) -> None:
    if value is None:
        _fail(f"{path} must not be null")
    if isinstance(value, float) and not math.isfinite(value):
        _fail(f"{path} contains a non-finite number")
    if isinstance(value, Mapping):
        for key, child in value.items():
            if not isinstance(key, str):
                _fail(f"{path} contains a non-string key")
            if _FORBIDDEN_KEY.search(key):
                _fail(f"{path}.{key} is forbidden in a shadow ledger")
            _reject_unsafe_material(child, f"{path}.{key}")
    elif isinstance(value, list):
        for index, child in enumerate(value):
            _reject_unsafe_material(child, f"{path}[{index}]")
    elif isinstance(value, str) and _URL.search(value):
        _fail(f"{path} contains a forbidden URL")


def _identity(value: Any, path: str) -> str:
    if not isinstance(value, str) or not _IDENTITY.fullmatch(value):
        _fail(f"{path} must be a lowercase immutable identity")
    return value


def _digest(value: Any, path: str) -> str:
    if not isinstance(value, str) or not _DIGEST.fullmatch(value):
        _fail(f"{path} must be a lowercase SHA-256 digest")
    return value


def _revision(value: Any, path: str) -> str:
    if not isinstance(value, str) or not _REVISION.fullmatch(value):
        _fail(f"{path} must be a lowercase 40-character revision")
    return value


def _timestamp(value: Any, path: str) -> datetime:
    if not isinstance(value, str) or not _TIMESTAMP.fullmatch(value):
        _fail(f"{path} must be an RFC3339 UTC timestamp with whole seconds")
    try:
        return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
    except ValueError as exc:
        raise ShadowLedgerError(f"{path} must be a valid calendar timestamp") from exc


def _session(value: Any, path: str) -> date:
    if not isinstance(value, str):
        _fail(f"{path} must be an ISO date")
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise ShadowLedgerError(f"{path} must be an ISO date") from exc


def _canonical_json(value: Mapping[str, Any], excluded_field: str, label: str) -> str:
    content = dict(value)
    content.pop(excluded_field, None)
    try:
        return json.dumps(content, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise ShadowLedgerError(f"{label} cannot be represented as canonical JSON") from exc


def calculate_input_sha256(value: Mapping[str, Any]) -> str:
    return hashlib.sha256(_canonical_json(value, "input_sha256", "shadow input").encode("utf-8")).hexdigest()


def calculate_decision_sha256(value: Mapping[str, Any]) -> str:
    return hashlib.sha256(_canonical_json(value, "decision_sha256", "forward decision").encode("utf-8")).hexdigest()


def calculate_receipt_sha256(value: Mapping[str, Any]) -> str:
    return hashlib.sha256(_canonical_json(value, "receipt_sha256", "shadow receipt").encode("utf-8")).hexdigest()


def calculate_policy_gate_receipt_sha256(value: Mapping[str, Any]) -> str:
    """Return the digest of the non-secret receipt emitted by the P0 KMS gate."""
    return hashlib.sha256(
        _canonical_json(value, "receipt_sha256", "policy-gate receipt").encode("utf-8")
    ).hexdigest()


def _allocation(value: Any, path: str) -> dict[str, int]:
    allocation = _object(value, path)
    _exact_keys(allocation, set(_ALLOCATION_SYMBOLS), path)
    normalized: dict[str, int] = {}
    for symbol in _ALLOCATION_SYMBOLS:
        amount = allocation[symbol]
        if isinstance(amount, bool) or not isinstance(amount, int) or not 0 <= amount <= 10_000:
            _fail(f"{path}.{symbol} must be an integer from 0 to 10000")
        normalized[symbol] = amount
    if sum(normalized.values()) != 10_000:
        _fail(f"{path} must sum to 10000 basis points")
    return normalized


def _adjustment_bps(value: Any, path: str) -> dict[str, int]:
    adjustment = _object(value, path)
    _exact_keys(adjustment, set(_ALLOCATION_SYMBOLS), path)
    normalized: dict[str, int] = {}
    for symbol in _ALLOCATION_SYMBOLS:
        amount = adjustment[symbol]
        if isinstance(amount, bool) or not isinstance(amount, int) or not -10_000 <= amount <= 10_000:
            _fail(f"{path}.{symbol} must be an integer from -10000 to 10000")
        normalized[symbol] = amount
    if sum(normalized.values()) != 0:
        _fail(f"{path} must sum to zero basis points")
    return normalized


def _candidate(value: Any) -> dict[str, str]:
    candidate = _object(value, "candidate")
    _exact_keys(candidate, _CANDIDATE_FIELDS, "candidate")
    if candidate["candidate_id"] != CANDIDATE_ID:
        _fail(f"candidate.candidate_id must be {CANDIDATE_ID}")
    repository = candidate["strategy_repository"]
    if not isinstance(repository, str) or "/" not in repository or _URL.search(repository):
        _fail("candidate.strategy_repository must be an owner/repository identity")
    return {
        "candidate_id": CANDIDATE_ID,
        "config_sha256": _digest(candidate["config_sha256"], "candidate.config_sha256"),
        "strategy_repository": repository,
        "strategy_revision": _revision(candidate["strategy_revision"], "candidate.strategy_revision"),
    }


def _source_evidence(value: Any, candidate: Mapping[str, str]) -> dict[str, str]:
    evidence = _object(value, "source_evidence")
    _exact_keys(evidence, _EVIDENCE_FIELDS, "source_evidence")
    if evidence["p2_config_sha256"] != candidate["config_sha256"]:
        _fail("source_evidence.p2_config_sha256 must match candidate.config_sha256")
    return {
        "p1_manifest_sha256": _digest(evidence["p1_manifest_sha256"], "source_evidence.p1_manifest_sha256"),
        "p2_config_sha256": candidate["config_sha256"],
        "p3_evidence_sha256": _digest(evidence["p3_evidence_sha256"], "source_evidence.p3_evidence_sha256"),
        "producer_revision": _revision(evidence["producer_revision"], "source_evidence.producer_revision"),
    }


def _forward_decision(value: Any, candidate: Mapping[str, str]) -> dict[str, Any]:
    decision = _object(value, "forward_decision")
    _exact_keys(decision, _DECISION_FIELDS, "forward_decision")
    if decision["producer_revision"] != candidate["strategy_revision"]:
        _fail("forward_decision.producer_revision must match candidate.strategy_revision")
    normalized = {
        "decision_id": _identity(decision["decision_id"], "forward_decision.decision_id"),
        "effective_session": decision["effective_session"],
        "producer_revision": candidate["strategy_revision"],
        "allocation_bps": _allocation(decision["allocation_bps"], "forward_decision.allocation_bps"),
        "decision_sha256": _digest(decision["decision_sha256"], "forward_decision.decision_sha256"),
    }
    _session(normalized["effective_session"], "forward_decision.effective_session")
    if normalized["decision_sha256"] != calculate_decision_sha256(normalized):
        _fail("forward_decision.decision_sha256 mismatch")
    return normalized


def _risk_control(value: Any) -> dict[str, str]:
    risk_control = _object(value, "risk_control")
    _exact_keys(risk_control, _RISK_CONTROL_FIELDS, "risk_control")
    if risk_control["stage"] != "SHADOW" or risk_control["execution_lane"] != "SHADOW_LEDGER":
        _fail("risk_control must be the SHADOW / SHADOW_LEDGER lane")
    return {
        "stage": "SHADOW",
        "execution_lane": "SHADOW_LEDGER",
        "risk_policy_id": _identity(risk_control["risk_policy_id"], "risk_control.risk_policy_id"),
        "risk_policy_version": _identity(risk_control["risk_policy_version"], "risk_control.risk_policy_version"),
        "risk_policy_sha256": _digest(risk_control["risk_policy_sha256"], "risk_control.risk_policy_sha256"),
    }


def _repository(value: Any, path: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*/[A-Za-z0-9][A-Za-z0-9_.-]*", value):
        _fail(f"{path} must be an owner/repository identity")
    return value


def _policy_gate_receipt(
    value: Any,
    *,
    observed_at: datetime,
    expected_bundle_sha256: str,
    risk_control: Mapping[str, str],
) -> dict[str, Any]:
    """Validate a bounded upstream policy-gate receipt for the P5 shadow lane.

    The receipt must arrive from the independently protected KMS gate. This
    consumer checks its closed structure, digest, control window, and exact
    risk-policy binding; it neither verifies a KMS signature nor issues a
    policy itself.
    """
    _reject_unsafe_material(value, "policy_gate_receipt")
    receipt = _object(value, "policy_gate_receipt")
    _exact_keys(receipt, _POLICY_GATE_RECEIPT_FIELDS, "policy_gate_receipt")
    if receipt["schema"] != POLICY_GATE_RECEIPT_SCHEMA:
        _fail(f"policy_gate_receipt.schema must be {POLICY_GATE_RECEIPT_SCHEMA}")
    verified_at_text = receipt["verified_at"]
    verified_at = _timestamp(verified_at_text, "policy_gate_receipt.verified_at")

    bundle = _object(receipt["deployment_bundle"], "policy_gate_receipt.deployment_bundle")
    _exact_keys(bundle, _POLICY_GATE_BUNDLE_FIELDS, "policy_gate_receipt.deployment_bundle")
    if bundle["schema"] != "qsl.deployment_bundle.v1":
        _fail("policy_gate_receipt.deployment_bundle.schema must be qsl.deployment_bundle.v1")
    normalized_bundle = {
        "schema": "qsl.deployment_bundle.v1",
        "bundle_id": _identity(bundle["bundle_id"], "policy_gate_receipt.deployment_bundle.bundle_id"),
        "bundle_sha256": _digest(bundle["bundle_sha256"], "policy_gate_receipt.deployment_bundle.bundle_sha256"),
    }
    if normalized_bundle["bundle_sha256"] != expected_bundle_sha256:
        _fail("policy_gate_receipt deployment bundle does not match shadow input")

    policy = _object(receipt["policy"], "policy_gate_receipt.policy")
    _exact_keys(policy, _POLICY_GATE_POLICY_FIELDS, "policy_gate_receipt.policy")
    if policy["stage"] != "SHADOW":
        _fail("policy_gate_receipt.policy.stage must be SHADOW")
    policy_effective_text = policy["effective_at"]
    policy_expires_text = policy["expires_at"]
    policy_effective = _timestamp(policy_effective_text, "policy_gate_receipt.policy.effective_at")
    policy_expires = _timestamp(policy_expires_text, "policy_gate_receipt.policy.expires_at")
    if policy_expires <= policy_effective:
        _fail("policy_gate_receipt policy window is invalid")
    normalized_policy = {
        "policy_id": _identity(policy["policy_id"], "policy_gate_receipt.policy.policy_id"),
        "policy_version": _identity(policy["policy_version"], "policy_gate_receipt.policy.policy_version"),
        "policy_sha256": _digest(policy["policy_sha256"], "policy_gate_receipt.policy.policy_sha256"),
        "stage": "SHADOW",
        "effective_at": policy_effective_text,
        "expires_at": policy_expires_text,
    }

    activation = _object(receipt["activation"], "policy_gate_receipt.activation")
    _exact_keys(activation, _POLICY_GATE_ACTIVATION_FIELDS, "policy_gate_receipt.activation")
    activation_effective_text = activation["effective_at"]
    activation_expires_text = activation["expires_at"]
    activation_effective = _timestamp(activation_effective_text, "policy_gate_receipt.activation.effective_at")
    activation_expires = _timestamp(activation_expires_text, "policy_gate_receipt.activation.expires_at")
    if activation_expires <= activation_effective:
        _fail("policy_gate_receipt activation window is invalid")
    if activation_effective < policy_effective or activation_expires > policy_expires:
        _fail("policy_gate_receipt activation window is not contained in policy window")
    normalized_activation = {
        "activation_id": _identity(activation["activation_id"], "policy_gate_receipt.activation.activation_id"),
        "activation_sha256": _digest(activation["activation_sha256"], "policy_gate_receipt.activation.activation_sha256"),
        "effective_at": activation_effective_text,
        "expires_at": activation_expires_text,
    }

    target = _object(receipt["target"], "policy_gate_receipt.target")
    _exact_keys(target, _POLICY_GATE_TARGET_FIELDS, "policy_gate_receipt.target")
    normalized_target = {
        "platform": _identity(target["platform"], "policy_gate_receipt.target.platform"),
        "repository": _repository(target["repository"], "policy_gate_receipt.target.repository"),
        "revision": _revision(target["revision"], "policy_gate_receipt.target.revision"),
        "environment": _identity(target["environment"], "policy_gate_receipt.target.environment"),
        "target_sha256": _digest(target["target_sha256"], "policy_gate_receipt.target.target_sha256"),
    }
    if normalized_target["platform"] != "alpaca" or normalized_target["repository"] != "QuantStrategyLab/AlpacaPlatform":
        _fail("policy_gate_receipt target must bind this Alpaca P5 gateway")
    if normalized_target["environment"] != "alpaca-shadow":
        _fail("policy_gate_receipt target.environment must be alpaca-shadow")

    receipt_risk = _object(receipt["risk_control"], "policy_gate_receipt.risk_control")
    _exact_keys(receipt_risk, _POLICY_GATE_RISK_FIELDS, "policy_gate_receipt.risk_control")
    normalized_risk = {
        "risk_policy_id": _identity(receipt_risk["risk_policy_id"], "policy_gate_receipt.risk_control.risk_policy_id"),
        "risk_policy_version": _identity(
            receipt_risk["risk_policy_version"], "policy_gate_receipt.risk_control.risk_policy_version"
        ),
        "risk_policy_sha256": _digest(
            receipt_risk["risk_policy_sha256"], "policy_gate_receipt.risk_control.risk_policy_sha256"
        ),
    }
    if normalized_risk != {
        "risk_policy_id": risk_control["risk_policy_id"],
        "risk_policy_version": risk_control["risk_policy_version"],
        "risk_policy_sha256": risk_control["risk_policy_sha256"],
    }:
        _fail("policy_gate_receipt risk control does not match shadow input")

    trusted_root = _object(receipt["trusted_policy_root"], "policy_gate_receipt.trusted_policy_root")
    _exact_keys(trusted_root, _POLICY_GATE_ROOT_FIELDS, "policy_gate_receipt.trusted_policy_root")
    root_expires_text = trusted_root["expires_at"]
    root_expires = _timestamp(root_expires_text, "policy_gate_receipt.trusted_policy_root.expires_at")
    normalized_root = {
        "root_id": _identity(trusted_root["root_id"], "policy_gate_receipt.trusted_policy_root.root_id"),
        "trusted_policy_root_sha256": _digest(
            trusted_root["trusted_policy_root_sha256"], "policy_gate_receipt.trusted_policy_root.trusted_policy_root_sha256"
        ),
        "expires_at": root_expires_text,
    }
    normalized: dict[str, Any] = {
        "schema": POLICY_GATE_RECEIPT_SCHEMA,
        "verified_at": verified_at_text,
        "deployment_bundle": normalized_bundle,
        "policy": normalized_policy,
        "activation": normalized_activation,
        "target": normalized_target,
        "risk_control": normalized_risk,
        "trusted_policy_root": normalized_root,
        "signature_sha256": _digest(receipt["signature_sha256"], "policy_gate_receipt.signature_sha256"),
        "receipt_sha256": _digest(receipt["receipt_sha256"], "policy_gate_receipt.receipt_sha256"),
    }
    if normalized["receipt_sha256"] != calculate_policy_gate_receipt_sha256(normalized):
        _fail("policy_gate_receipt.receipt_sha256 mismatch")
    if observed_at < verified_at or observed_at < activation_effective:
        _fail("policy_gate_receipt is not yet effective for this shadow cycle")
    if observed_at >= min(policy_expires, activation_expires, root_expires):
        _fail("policy_gate_receipt is expired for this shadow cycle")
    return normalized


def validate_shadow_cycle_input(value: Any) -> dict[str, Any]:
    """Validate one bounded P5 input; this does not verify or issue a KMS policy."""
    _reject_unsafe_material(value, "shadow_input")
    root = _object(value, "shadow_input")
    _exact_keys(root, _INPUT_FIELDS, "shadow_input")
    if root["schema"] != INPUT_SCHEMA:
        _fail(f"shadow_input.schema must be {INPUT_SCHEMA}")
    candidate = _candidate(root["candidate"])
    produced_at = _timestamp(root["produced_at"], "shadow_input.produced_at")
    deployment_bundle_sha256 = _digest(root["deployment_bundle_sha256"], "shadow_input.deployment_bundle_sha256")
    risk_control = _risk_control(root["risk_control"])
    normalized = {
        "schema": INPUT_SCHEMA,
        "cycle_id": _identity(root["cycle_id"], "shadow_input.cycle_id"),
        "produced_at": root["produced_at"],
        "deployment_bundle_sha256": deployment_bundle_sha256,
        "candidate": candidate,
        "source_evidence": _source_evidence(root["source_evidence"], candidate),
        "forward_decision": _forward_decision(root["forward_decision"], candidate),
        "risk_control": risk_control,
        "policy_gate_receipt": _policy_gate_receipt(
            root["policy_gate_receipt"],
            observed_at=produced_at,
            expected_bundle_sha256=deployment_bundle_sha256,
            risk_control=risk_control,
        ),
        "input_sha256": _digest(root["input_sha256"], "shadow_input.input_sha256"),
    }
    if normalized["input_sha256"] != calculate_input_sha256(normalized):
        _fail("shadow_input.input_sha256 mismatch")
    return normalized


def validate_shadow_ledger_receipt(value: Any) -> dict[str, Any]:
    """Validate a receipt written by this module before using it as a ledger parent."""
    _reject_unsafe_material(value, "shadow_receipt")
    root = _object(value, "shadow_receipt")
    _exact_keys(root, _RECEIPT_FIELDS, "shadow_receipt")
    if root["schema"] != RECEIPT_SCHEMA:
        _fail(f"shadow_receipt.schema must be {RECEIPT_SCHEMA}")
    candidate = _candidate(root["candidate"])
    produced_at = _timestamp(root["produced_at"], "shadow_receipt.produced_at")
    deployment_bundle_sha256 = _digest(root["deployment_bundle_sha256"], "shadow_receipt.deployment_bundle_sha256")
    risk_control = _risk_control(root["risk_control"])
    normalized = {
        "schema": RECEIPT_SCHEMA,
        "cycle_id": _identity(root["cycle_id"], "shadow_receipt.cycle_id"),
        "produced_at": root["produced_at"],
        "deployment_bundle_sha256": deployment_bundle_sha256,
        "candidate": candidate,
        "source_evidence": _source_evidence(root["source_evidence"], candidate),
        "forward_decision": _forward_decision(root["forward_decision"], candidate),
        "risk_control": risk_control,
        "policy_gate_receipt": _policy_gate_receipt(
            root["policy_gate_receipt"],
            observed_at=produced_at,
            expected_bundle_sha256=deployment_bundle_sha256,
            risk_control=risk_control,
        ),
        "ledger_parent_sha256": _digest(root["ledger_parent_sha256"], "shadow_receipt.ledger_parent_sha256"),
        "shadow_adjustments_bps": _adjustment_bps(
            root["shadow_adjustments_bps"], "shadow_receipt.shadow_adjustments_bps"
        ),
        "receipt_sha256": _digest(root["receipt_sha256"], "shadow_receipt.receipt_sha256"),
    }
    if normalized["receipt_sha256"] != calculate_receipt_sha256(normalized):
        _fail("shadow_receipt.receipt_sha256 mismatch")
    return normalized


def _adjustments(current: Mapping[str, int], previous: Mapping[str, int]) -> dict[str, int]:
    return {symbol: current[symbol] - previous[symbol] for symbol in _ALLOCATION_SYMBOLS}


def build_shadow_ledger_receipt(cycle_input: Any, *, prior_receipt: Any | None = None) -> dict[str, Any]:
    """Build a chain-linked virtual P5 receipt without an external side effect."""
    cycle = validate_shadow_cycle_input(cycle_input)
    previous_allocation = {"TQQQ": 0, "QQQM": 0, "BOXX": 0, "CASH": 10_000}
    parent_sha256 = _GENESIS_DIGEST
    if prior_receipt is not None:
        parent = validate_shadow_ledger_receipt(prior_receipt)
        if parent["candidate"] != cycle["candidate"]:
            _fail("prior receipt candidate does not match shadow input")
        current_session = _session(cycle["forward_decision"]["effective_session"], "forward_decision.effective_session")
        prior_session = _session(
            parent["forward_decision"]["effective_session"],
            "prior_receipt.forward_decision.effective_session",
        )
        if current_session <= prior_session:
            _fail("shadow effective session must advance beyond the prior receipt")
        previous_allocation = parent["forward_decision"]["allocation_bps"]
        parent_sha256 = parent["receipt_sha256"]
    receipt: dict[str, Any] = {
        "schema": RECEIPT_SCHEMA,
        "cycle_id": cycle["cycle_id"],
        "produced_at": cycle["produced_at"],
        "deployment_bundle_sha256": cycle["deployment_bundle_sha256"],
        "candidate": cycle["candidate"],
        "source_evidence": cycle["source_evidence"],
        "forward_decision": cycle["forward_decision"],
        "risk_control": cycle["risk_control"],
        "policy_gate_receipt": cycle["policy_gate_receipt"],
        "ledger_parent_sha256": parent_sha256,
        "shadow_adjustments_bps": _adjustments(cycle["forward_decision"]["allocation_bps"], previous_allocation),
        "receipt_sha256": "",
    }
    receipt["receipt_sha256"] = calculate_receipt_sha256(receipt)
    return validate_shadow_ledger_receipt(receipt)


def _no_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            _fail(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _load_json(path: Path, label: str) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=_no_duplicate_keys)
    except (OSError, json.JSONDecodeError) as exc:
        raise ShadowLedgerError(f"invalid {label} JSON") from exc


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build one pure, no-broker TQQQ P5 shadow ledger receipt")
    parser.add_argument("--input", type=Path, required=True, help="shadow-cycle input JSON")
    parser.add_argument("--output", type=Path, required=True, help="new create-only receipt path")
    parser.add_argument("--prior-receipt", type=Path, help="optional preceding receipt JSON")
    args = parser.parse_args(argv)
    try:
        receipt = build_shadow_ledger_receipt(
            _load_json(args.input, "shadow input"),
            prior_receipt=_load_json(args.prior_receipt, "prior receipt") if args.prior_receipt else None,
        )
        with args.output.open("x", encoding="utf-8") as handle:
            handle.write(json.dumps(receipt, sort_keys=True, separators=(",", ":")))
    except (OSError, ShadowLedgerError) as exc:
        print(f"shadow ledger failed: {exc}", file=sys.stderr)
        return 1
    print(f"SHADOW_RECEIPT_RECORDED cycle={receipt['cycle_id']} receipt_sha256={receipt['receipt_sha256']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
