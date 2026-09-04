"""Local, fail-closed admission contract for Alpaca paper dry-runs."""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from typing import Any, Mapping

PAPER_ADMISSION_SCHEMA = "qsl.alpaca_paper_admission.v1"
PAPER_ENVIRONMENT = "PAPER_DRY_RUN"

_DIGEST = re.compile(r"[0-9a-f]{64}")
_IDENTITY = re.compile(r"[a-z0-9][a-z0-9._:-]{0,127}")
_TIMESTAMP = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z")
_FORBIDDEN_KEY = re.compile(
    r"(?:account|order|credential|secret|token|password|authorization|shadow)",
    re.IGNORECASE,
)
_FIELDS = {
    "schema",
    "environment",
    "cycle_id",
    "valid_from",
    "valid_until",
    "paper_endpoint_sha256",
    "config_sha256",
    "strategy_sha256",
    "deployment_sha256",
    "risk_sha256",
    "admission_sha256",
}
_BODY_FIELDS = _FIELDS - {"admission_sha256"}


class PaperAdmissionError(ValueError):
    """Raised when a paper admission is invalid or unsafe."""


class PaperAdmissionConflictError(PaperAdmissionError):
    """Raised when one cycle id is associated with different admission data."""


def _fail(message: str) -> None:
    raise PaperAdmissionError(message)


def _object(value: Any, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        _fail(f"{label} must be an object")
    return value


def _check_keys(value: Mapping[str, Any], required: set[str], allowed: set[str], label: str) -> None:
    keys = set(value)
    if any(not isinstance(key, str) for key in keys):
        _fail(f"{label} contains a non-string field name")
    missing = sorted(required - keys)
    unknown = sorted(keys - allowed)
    if missing:
        _fail(f"{label} missing required field(s): {', '.join(missing)}")
    if unknown:
        if any(_FORBIDDEN_KEY.search(key) for key in unknown):
            _fail(f"{label} contains a forbidden field")
        _fail(f"{label} contains an unknown field")


def _exact_keys(value: Mapping[str, Any], expected: set[str], label: str) -> None:
    _check_keys(value, expected, expected, label)


def _digest(value: Any, label: str) -> str:
    if not isinstance(value, str) or not _DIGEST.fullmatch(value):
        _fail(f"{label} must be a lowercase SHA-256 digest")
    return value


def _timestamp(value: Any, label: str) -> datetime:
    if not isinstance(value, str) or not _TIMESTAMP.fullmatch(value):
        _fail(f"{label} must be an RFC3339 UTC timestamp with whole seconds")
    try:
        return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except ValueError as exc:
        raise PaperAdmissionError(f"{label} must be a valid calendar timestamp") from exc


def _canonical_json(value: Mapping[str, Any]) -> str:
    content = dict(value)
    content.pop("admission_sha256", None)
    try:
        return json.dumps(content, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise PaperAdmissionError("paper admission cannot be represented as canonical JSON") from exc


def calculate_paper_admission_sha256(value: Mapping[str, Any]) -> str:
    """Calculate the digest over all admission fields except its self-digest."""
    admission = _object(value, "paper admission")
    _check_keys(admission, _BODY_FIELDS, _BODY_FIELDS | {"admission_sha256"}, "paper admission")
    return hashlib.sha256(_canonical_json(admission).encode("utf-8")).hexdigest()


def _validate_body(value: Mapping[str, Any]) -> None:
    if value["schema"] != PAPER_ADMISSION_SCHEMA:
        _fail("paper admission schema is unsupported")
    if value["environment"] != PAPER_ENVIRONMENT:
        _fail("paper admission environment is not PAPER_DRY_RUN")
    if not isinstance(value["cycle_id"], str) or not _IDENTITY.fullmatch(value["cycle_id"]):
        _fail("paper admission cycle_id must be an immutable lowercase identity")

    valid_from = _timestamp(value["valid_from"], "paper admission valid_from")
    valid_until = _timestamp(value["valid_until"], "paper admission valid_until")
    if valid_from >= valid_until:
        _fail("paper admission validity window must be increasing")

    for field in (
        "paper_endpoint_sha256",
        "config_sha256",
        "strategy_sha256",
        "deployment_sha256",
        "risk_sha256",
        "admission_sha256",
    ):
        _digest(value[field], f"paper admission {field}")


def validate_paper_admission(value: Any) -> dict[str, str]:
    """Validate and return a copy of a complete local paper admission."""
    admission = _object(value, "paper admission")
    _exact_keys(admission, _FIELDS, "paper admission")
    _validate_body(admission)
    if admission["admission_sha256"] != calculate_paper_admission_sha256(admission):
        _fail("paper admission digest does not match its content")
    return dict(admission)


def build_paper_admission(
    *,
    environment: str,
    cycle_id: str,
    valid_from: str,
    valid_until: str,
    paper_endpoint_sha256: str,
    config_sha256: str,
    strategy_sha256: str,
    deployment_sha256: str,
    risk_sha256: str,
) -> dict[str, str]:
    """Build a deterministic, non-executable paper admission."""
    admission: dict[str, str] = {
        "schema": PAPER_ADMISSION_SCHEMA,
        "environment": environment,
        "cycle_id": cycle_id,
        "valid_from": valid_from,
        "valid_until": valid_until,
        "paper_endpoint_sha256": paper_endpoint_sha256,
        "config_sha256": config_sha256,
        "strategy_sha256": strategy_sha256,
        "deployment_sha256": deployment_sha256,
        "risk_sha256": risk_sha256,
        "admission_sha256": "0" * 64,
    }
    _exact_keys(admission, _FIELDS, "paper admission")
    _validate_body(admission)
    admission["admission_sha256"] = calculate_paper_admission_sha256(admission)
    return admission


def reconcile_paper_admission(existing: Any, candidate: Any) -> str:
    """Reconcile two local admissions for one idempotency key."""
    current = validate_paper_admission(existing)
    proposed = validate_paper_admission(candidate)
    if (
        current["cycle_id"] != proposed["cycle_id"]
        or current["admission_sha256"] != proposed["admission_sha256"]
    ):
        raise PaperAdmissionConflictError("paper admission cycle id already has different content")
    return "RECONCILED"
