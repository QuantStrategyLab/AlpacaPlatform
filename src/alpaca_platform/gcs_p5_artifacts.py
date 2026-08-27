"""Narrow Google Cloud Storage adapters for P5 shadow artifacts.

The P5 controller intentionally owns all candidate, policy, receipt, and risk
validation.  These adapters only move one already-bounded JSON snapshot and
one already-validated admission through GCS.  They never discover a latest
cycle, list a bucket, accept a URL, read credentials from configuration, or
touch a broker.

Production code injects a ``google.cloud.storage.Bucket`` bound to a workload
identity.  Keeping the bucket client injected makes the data boundary explicit
and keeps this module testable without a cloud SDK or network access.
"""

from __future__ import annotations

import copy
import json
import re
from collections.abc import Mapping
from typing import Any, Protocol

from .p5_default_parked_scheduler import P5ShadowArtifactSnapshot, RestrictedP5ArtifactReadError
from .shadow_receipt_store import ShadowReceiptStoreError, validate_shadow_receipt_admission

_CYCLE_ID = re.compile(r"^[a-z][a-z0-9]*(?:[._-][a-z0-9]+)*$")
_PREFIX = re.compile(r"^[a-z0-9][a-z0-9._/-]*[a-z0-9]$")
_REQUEST_FIELDS = {
    "cycle_id",
    "forward_observation",
    "policy_gate_receipt",
    "risk_control",
    "deployment_bundle_sha256",
    "risk_gate_decision",
    "prior_receipt",
}


class GcsP5ArtifactError(ShadowReceiptStoreError):
    """A sanitized GCS adapter error safe for the P5 fail-closed boundary."""


class _Blob(Protocol):
    def download_as_bytes(self) -> bytes:
        """Return the complete immutable JSON object."""

    def upload_from_string(self, data: str, **kwargs: Any) -> None:
        """Create one JSON object, honoring the supplied generation precondition."""


class _Bucket(Protocol):
    def blob(self, blob_name: str) -> _Blob:
        """Return a handle for exactly one caller-derived object name."""


def _cycle_id(value: Any) -> str:
    if not isinstance(value, str) or not _CYCLE_ID.fullmatch(value):
        raise GcsP5ArtifactError("P5 cycle_id must be a lowercase immutable identity")
    return value


def _prefix(value: Any, *, label: str) -> str:
    if not isinstance(value, str):
        raise GcsP5ArtifactError(f"{label} must be a safe relative object prefix")
    normalized = value.strip().strip("/")
    if (
        not normalized
        or "//" in normalized
        or ".." in normalized
        or any(part == "." for part in normalized.split("/"))
        or not _PREFIX.fullmatch(normalized)
    ):
        raise GcsP5ArtifactError(f"{label} must be a safe relative object prefix")
    return normalized


def _object_name(prefix: str, cycle_id: str) -> str:
    return f"{prefix}/{cycle_id}.json"


def _is_status(error: BaseException, expected: int) -> bool:
    values = [getattr(error, name, None) for name in ("code", "status_code")]
    response = getattr(error, "response", None)
    if response is not None:
        values.extend(getattr(response, name, None) for name in ("code", "status_code"))
    for value in values:
        if callable(value):
            try:
                value = value()
            except TypeError:
                continue
        if value == expected:
            return True
    return False


def _json_object(value: Any, *, label: str) -> dict[str, Any]:
    if not isinstance(value, (bytes, bytearray)):
        raise GcsP5ArtifactError(f"{label} must be UTF-8 JSON bytes")
    if len(value) > 1_000_000:
        raise GcsP5ArtifactError(f"{label} exceeds the bounded P5 artifact size")
    try:
        decoded = json.loads(bytes(value).decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise GcsP5ArtifactError(f"{label} is not valid JSON") from exc
    if not isinstance(decoded, dict):
        raise GcsP5ArtifactError(f"{label} must be a JSON object")
    return decoded


def _snapshot_from_request(value: Mapping[str, Any], *, expected_cycle_id: str) -> P5ShadowArtifactSnapshot:
    missing = sorted(_REQUEST_FIELDS - set(value))
    unknown = sorted(set(value) - _REQUEST_FIELDS)
    if missing or unknown:
        raise GcsP5ArtifactError("P5 artifact request has an invalid field set")
    if _cycle_id(value["cycle_id"]) != expected_cycle_id:
        raise GcsP5ArtifactError("P5 artifact request cycle_id does not match the requested cycle")
    # Copy before handing the values to the controller so an SDK/fake caller
    # cannot mutate the received snapshot after this adapter returns it.
    try:
        copied = copy.deepcopy(dict(value))
    except Exception as exc:  # pragma: no cover - JSON input is normally copyable
        raise GcsP5ArtifactError("P5 artifact request cannot be isolated") from exc
    return P5ShadowArtifactSnapshot(
        cycle_id=expected_cycle_id,
        forward_observation=copied["forward_observation"],
        policy_gate_receipt=copied["policy_gate_receipt"],
        risk_control=copied["risk_control"],
        deployment_bundle_sha256=copied["deployment_bundle_sha256"],
        risk_gate_decision=copied["risk_gate_decision"],
        prior_receipt=copied["prior_receipt"],
    )


class GcsP5ShadowArtifactReader:
    """Read exactly one immutable P5 input request by cycle id.

    A missing request is a normal ``None`` result.  Any other storage or shape
    issue is deliberately collapsed to :class:`RestrictedP5ArtifactReadError`;
    the default-PARKED controller turns it into a sanitized status without
    publishing provider paths or error details.
    """

    def __init__(self, bucket: _Bucket, *, request_prefix: str = "p5-inputs") -> None:
        if not callable(getattr(bucket, "blob", None)):
            raise GcsP5ArtifactError("P5 artifact bucket must provide blob()")
        self._bucket = bucket
        self._request_prefix = _prefix(request_prefix, label="P5 request_prefix")

    def read_snapshot(self, *, cycle_id: str) -> P5ShadowArtifactSnapshot | None:
        normalized_cycle_id = _cycle_id(cycle_id)
        try:
            payload = self._bucket.blob(
                _object_name(self._request_prefix, normalized_cycle_id)
            ).download_as_bytes()
        except Exception as exc:  # noqa: BLE001 - provider exceptions must fail closed.
            if _is_status(exc, 404):
                return None
            raise RestrictedP5ArtifactReadError("P5 artifact snapshot is unavailable") from None
        try:
            return _snapshot_from_request(
                _json_object(payload, label="P5 artifact request"),
                expected_cycle_id=normalized_cycle_id,
            )
        except GcsP5ArtifactError as exc:
            raise RestrictedP5ArtifactReadError("P5 artifact snapshot is invalid") from exc


class GcsCreateOnlyShadowReceiptStore:
    """Persist validated P5 admissions with GCS generation-match creation.

    The runtime identity needs only object read and object create on this
    prefix.  It never lists, overwrites, or deletes objects.  A precondition
    collision returns ``False`` so the P5 admission layer can re-read and
    reconcile the immutable stored admission.
    """

    def __init__(self, bucket: _Bucket, *, receipt_prefix: str = "p5-receipts") -> None:
        if not callable(getattr(bucket, "blob", None)):
            raise GcsP5ArtifactError("P5 receipt bucket must provide blob()")
        self._bucket = bucket
        self._receipt_prefix = _prefix(receipt_prefix, label="P5 receipt_prefix")

    def read(self, cycle_id: str) -> dict[str, Any] | None:
        normalized_cycle_id = _cycle_id(cycle_id)
        try:
            payload = self._bucket.blob(
                _object_name(self._receipt_prefix, normalized_cycle_id)
            ).download_as_bytes()
        except Exception as exc:  # noqa: BLE001 - provider exceptions must fail closed.
            if _is_status(exc, 404):
                return None
            raise GcsP5ArtifactError("P5 receipt object is unavailable") from None
        try:
            admission = validate_shadow_receipt_admission(
                _json_object(payload, label="P5 receipt admission")
            )
        except GcsP5ArtifactError:
            raise
        except ShadowReceiptStoreError as exc:
            raise GcsP5ArtifactError("P5 receipt object is invalid") from exc
        if admission["cycle_id"] != normalized_cycle_id:
            raise GcsP5ArtifactError("P5 receipt object cycle_id does not match the requested cycle")
        return admission

    def create_if_absent(self, admission: Mapping[str, Any]) -> bool:
        normalized = validate_shadow_receipt_admission(admission)
        payload = json.dumps(
            normalized,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            allow_nan=False,
        )
        try:
            self._bucket.blob(
                _object_name(self._receipt_prefix, normalized["cycle_id"])
            ).upload_from_string(
                payload,
                content_type="application/json",
                if_generation_match=0,
            )
        except Exception as exc:  # noqa: BLE001 - provider exceptions must fail closed.
            if _is_status(exc, 412):
                return False
            raise GcsP5ArtifactError("P5 receipt object could not be created") from None
        return True
