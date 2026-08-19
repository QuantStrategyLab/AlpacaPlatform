"""Pure, no-broker P5 shadow-ledger receipt construction for frozen TQQQ input.

There is intentionally no Alpaca client in this module. The caller supplies a
validated forward decision and writes the resulting receipt to its own
create-only storage. The module neither reads credentials nor makes network
requests nor produces broker order payloads.
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

INPUT_SCHEMA = "qsl.tqqq_shadow_cycle_input.v1"
RECEIPT_SCHEMA = "qsl.tqqq_shadow_ledger_receipt.v1"
CANDIDATE_ID = "tqqq-core-only-p2-v5"
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
    "candidate",
    "source_evidence",
    "forward_decision",
    "risk_control",
    "input_sha256",
}
_CANDIDATE_FIELDS = {"candidate_id", "config_sha256", "strategy_repository", "strategy_revision"}
_EVIDENCE_FIELDS = {"p1_manifest_sha256", "p2_config_sha256", "p3_evidence_sha256", "producer_revision"}
_DECISION_FIELDS = {"decision_id", "effective_session", "producer_revision", "allocation_bps", "decision_sha256"}
_RISK_CONTROL_FIELDS = {"stage", "execution_lane", "risk_policy_id", "risk_policy_version", "risk_policy_sha256"}
_RECEIPT_FIELDS = {
    "schema", "cycle_id", "produced_at", "candidate", "source_evidence", "forward_decision", "risk_control",
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


def validate_shadow_cycle_input(value: Any) -> dict[str, Any]:
    """Validate one bounded P5 input; this does not verify or issue its policy."""
    _reject_unsafe_material(value, "shadow_input")
    root = _object(value, "shadow_input")
    _exact_keys(root, _INPUT_FIELDS, "shadow_input")
    if root["schema"] != INPUT_SCHEMA:
        _fail(f"shadow_input.schema must be {INPUT_SCHEMA}")
    candidate = _candidate(root["candidate"])
    normalized = {
        "schema": INPUT_SCHEMA,
        "cycle_id": _identity(root["cycle_id"], "shadow_input.cycle_id"),
        "produced_at": root["produced_at"],
        "candidate": candidate,
        "source_evidence": _source_evidence(root["source_evidence"], candidate),
        "forward_decision": _forward_decision(root["forward_decision"], candidate),
        "risk_control": _risk_control(root["risk_control"]),
        "input_sha256": _digest(root["input_sha256"], "shadow_input.input_sha256"),
    }
    _timestamp(normalized["produced_at"], "shadow_input.produced_at")
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
    normalized = {
        "schema": RECEIPT_SCHEMA,
        "cycle_id": _identity(root["cycle_id"], "shadow_receipt.cycle_id"),
        "produced_at": root["produced_at"],
        "candidate": candidate,
        "source_evidence": _source_evidence(root["source_evidence"], candidate),
        "forward_decision": _forward_decision(root["forward_decision"], candidate),
        "risk_control": _risk_control(root["risk_control"]),
        "ledger_parent_sha256": _digest(root["ledger_parent_sha256"], "shadow_receipt.ledger_parent_sha256"),
        "shadow_adjustments_bps": _adjustment_bps(
            root["shadow_adjustments_bps"], "shadow_receipt.shadow_adjustments_bps"
        ),
        "receipt_sha256": _digest(root["receipt_sha256"], "shadow_receipt.receipt_sha256"),
    }
    _timestamp(normalized["produced_at"], "shadow_receipt.produced_at")
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
        "candidate": cycle["candidate"],
        "source_evidence": cycle["source_evidence"],
        "forward_decision": cycle["forward_decision"],
        "risk_control": cycle["risk_control"],
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
