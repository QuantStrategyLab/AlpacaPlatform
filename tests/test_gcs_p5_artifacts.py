from __future__ import annotations

import json
from typing import Any

import pytest

from alpaca_platform import gcs_p5_artifacts
from alpaca_platform.gcs_p5_artifacts import (
    GcsCreateOnlyShadowReceiptStore,
    GcsP5ArtifactError,
    GcsP5ShadowArtifactReader,
)
from alpaca_platform.p5_default_parked_scheduler import RestrictedP5ArtifactReadError
from alpaca_platform.shadow_receipt_store import ShadowReceiptStoreError


class FakeGcsError(Exception):
    def __init__(self, status_code: int) -> None:
        super().__init__(f"provider status {status_code}")
        self.status_code = status_code


class FakeBlob:
    def __init__(self, bucket: FakeBucket, name: str) -> None:
        self._bucket = bucket
        self._name = name

    def download_as_bytes(self) -> bytes:
        self._bucket.downloaded.append(self._name)
        if self._bucket.read_error is not None:
            raise self._bucket.read_error
        try:
            return self._bucket.objects[self._name]
        except KeyError as exc:
            raise FakeGcsError(404) from exc

    def upload_from_string(self, data: str, **kwargs: Any) -> None:
        self._bucket.uploaded.append((self._name, data, kwargs))
        if self._bucket.write_error is not None:
            raise self._bucket.write_error
        if kwargs.get("if_generation_match") == 0 and self._name in self._bucket.objects:
            raise FakeGcsError(412)
        self._bucket.objects[self._name] = data.encode("utf-8")


class FakeBucket:
    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}
        self.downloaded: list[str] = []
        self.uploaded: list[tuple[str, str, dict[str, Any]]] = []
        self.read_error: Exception | None = None
        self.write_error: Exception | None = None

    def blob(self, blob_name: str) -> FakeBlob:
        return FakeBlob(self, blob_name)


def snapshot_payload(cycle_id: str) -> dict[str, object]:
    return {
        "cycle_id": cycle_id,
        "forward_observation": {"opaque": "forward"},
        "policy_gate_receipt": {"opaque": "policy"},
        "risk_control": {"opaque": "risk"},
        "deployment_bundle_sha256": "a" * 64,
        "risk_gate_decision": {"opaque": "decision"},
        "prior_receipt": None,
    }


def test_reader_reads_one_exact_cycle_without_listing_or_discovery():
    bucket = FakeBucket()
    cycle_id = "tqqq_core_only_p2_v5_shadow_20260820"
    payload = snapshot_payload(cycle_id)
    bucket.objects[f"p5-inputs/{cycle_id}.json"] = json.dumps(payload).encode("utf-8")

    snapshot = GcsP5ShadowArtifactReader(bucket).read_snapshot(cycle_id=cycle_id)

    assert snapshot is not None
    assert snapshot.cycle_id == cycle_id
    assert snapshot.forward_observation == {"opaque": "forward"}
    assert bucket.downloaded == [f"p5-inputs/{cycle_id}.json"]


def test_reader_missing_or_malformed_objects_fail_closed():
    bucket = FakeBucket()
    cycle_id = "tqqq_core_only_p2_v5_shadow_20260820"
    reader = GcsP5ShadowArtifactReader(bucket)

    assert reader.read_snapshot(cycle_id=cycle_id) is None

    bucket.objects[f"p5-inputs/{cycle_id}.json"] = b"[]"
    with pytest.raises(RestrictedP5ArtifactReadError, match="snapshot is invalid"):
        reader.read_snapshot(cycle_id=cycle_id)

    bucket.read_error = FakeGcsError(503)
    with pytest.raises(RestrictedP5ArtifactReadError, match="snapshot is unavailable"):
        reader.read_snapshot(cycle_id=cycle_id)


def test_reader_rejects_unsafe_identifiers_and_prefixes():
    with pytest.raises(GcsP5ArtifactError, match="cycle_id"):
        GcsP5ShadowArtifactReader(FakeBucket()).read_snapshot(cycle_id="../latest")
    with pytest.raises(GcsP5ArtifactError, match="request_prefix"):
        GcsP5ShadowArtifactReader(FakeBucket(), request_prefix="p5-inputs/../other")
    with pytest.raises(GcsP5ArtifactError, match="request_prefix"):
        GcsP5ShadowArtifactReader(FakeBucket(), request_prefix="p5-inputs/./other")


def test_receipt_store_is_create_only_and_uses_a_generation_precondition(monkeypatch):
    bucket = FakeBucket()
    cycle_id = "tqqq_core_only_p2_v5_shadow_20260820"
    admission = {"cycle_id": cycle_id, "opaque": "already validated upstream"}
    monkeypatch.setattr(
        gcs_p5_artifacts,
        "validate_shadow_receipt_admission",
        lambda value: dict(value),
    )
    store = GcsCreateOnlyShadowReceiptStore(bucket)

    assert store.read(cycle_id) is None
    assert store.create_if_absent(admission) is True
    assert store.create_if_absent(admission) is False
    assert store.read(cycle_id) == admission
    assert [item[0] for item in bucket.uploaded] == [
        f"p5-receipts/{cycle_id}.json",
        f"p5-receipts/{cycle_id}.json",
    ]
    assert bucket.uploaded[0][2] == {
        "content_type": "application/json",
        "if_generation_match": 0,
    }
    assert json.loads(bucket.objects[f"p5-receipts/{cycle_id}.json"]) == admission


def test_receipt_store_returns_bounded_errors_for_unavailable_or_invalid_objects(monkeypatch):
    bucket = FakeBucket()
    cycle_id = "tqqq_core_only_p2_v5_shadow_20260820"
    admission = {"cycle_id": cycle_id}
    store = GcsCreateOnlyShadowReceiptStore(bucket)
    monkeypatch.setattr(
        gcs_p5_artifacts,
        "validate_shadow_receipt_admission",
        lambda value: dict(value),
    )

    bucket.write_error = FakeGcsError(503)
    with pytest.raises(ShadowReceiptStoreError, match="could not be created"):
        store.create_if_absent(admission)

    bucket.write_error = None
    bucket.objects[f"p5-receipts/{cycle_id}.json"] = b"{}"
    monkeypatch.setattr(
        gcs_p5_artifacts,
        "validate_shadow_receipt_admission",
        lambda _value: (_ for _ in ()).throw(ShadowReceiptStoreError("invalid admission")),
    )
    with pytest.raises(GcsP5ArtifactError, match="receipt object is invalid"):
        store.read(cycle_id)
