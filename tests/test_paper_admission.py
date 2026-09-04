from __future__ import annotations

import copy

import pytest

from alpaca_platform import paper_admission, shadow_ledger


def sha(character: str) -> str:
    return character * 64


def valid_admission() -> dict[str, str]:
    return paper_admission.build_paper_admission(
        environment="PAPER_DRY_RUN",
        cycle_id="alpaca_paper_20260904_001",
        valid_from="2026-09-04T00:00:00Z",
        valid_until="2026-09-05T00:00:00Z",
        paper_endpoint_sha256=sha("1"),
        config_sha256=sha("2"),
        strategy_sha256=sha("3"),
        deployment_sha256=sha("4"),
        risk_sha256=sha("5"),
    )


def test_build_and_validate_paper_admission():
    admission = valid_admission()

    assert admission["schema"] == paper_admission.PAPER_ADMISSION_SCHEMA
    assert admission["environment"] == "PAPER_DRY_RUN"
    assert paper_admission.validate_paper_admission(admission) == admission


def test_digest_calculation_allows_omitting_self_digest():
    admission = valid_admission()
    admission_without_digest = dict(admission)
    del admission_without_digest["admission_sha256"]

    assert paper_admission.calculate_paper_admission_sha256(admission_without_digest) == admission["admission_sha256"]


def test_same_cycle_and_content_is_idempotently_reconciled():
    admission = valid_admission()
    replay = copy.deepcopy(admission)

    assert paper_admission.reconcile_paper_admission(admission, replay) == "RECONCILED"


def test_same_cycle_with_changed_digest_is_a_conflict():
    admission = valid_admission()
    conflicting = copy.deepcopy(admission)
    conflicting["risk_sha256"] = sha("6")
    conflicting["admission_sha256"] = paper_admission.calculate_paper_admission_sha256(conflicting)

    with pytest.raises(paper_admission.PaperAdmissionConflictError):
        paper_admission.reconcile_paper_admission(admission, conflicting)


@pytest.mark.parametrize("environment", ["LIVE", "live", "SHADOW", "PAPER"])
def test_non_paper_environment_is_rejected(environment: str):
    admission = valid_admission()
    admission["environment"] = environment
    admission["admission_sha256"] = paper_admission.calculate_paper_admission_sha256(admission)

    with pytest.raises(paper_admission.PaperAdmissionError):
        paper_admission.validate_paper_admission(admission)


def test_shadow_ledger_schema_is_not_paper_admission():
    admission = valid_admission()
    admission["schema"] = shadow_ledger.RECEIPT_SCHEMA
    admission["admission_sha256"] = paper_admission.calculate_paper_admission_sha256(admission)

    with pytest.raises(paper_admission.PaperAdmissionError, match="schema is unsupported"):
        paper_admission.validate_paper_admission(admission)


def test_missing_paper_provenance_is_rejected():
    admission = valid_admission()
    del admission["environment"]

    with pytest.raises(paper_admission.PaperAdmissionError, match="missing required field"):
        paper_admission.validate_paper_admission(admission)


@pytest.mark.parametrize("forbidden_field", ["shadow_receipt", "account_id", "order_id", "credential"])
def test_shadow_and_sensitive_material_cannot_be_added(forbidden_field: str):
    admission = valid_admission()
    admission[forbidden_field] = "forbidden"

    with pytest.raises(paper_admission.PaperAdmissionError):
        paper_admission.validate_paper_admission(admission)


def test_raw_endpoint_and_missing_digest_are_rejected():
    admission = valid_admission()
    admission["endpoint"] = "https://paper.example.invalid"

    with pytest.raises(paper_admission.PaperAdmissionError):
        paper_admission.validate_paper_admission(admission)

    admission = valid_admission()
    del admission["risk_sha256"]
    with pytest.raises(paper_admission.PaperAdmissionError):
        paper_admission.validate_paper_admission(admission)


def test_window_and_cycle_id_are_fail_closed():
    admission = valid_admission()
    admission["valid_until"] = admission["valid_from"]
    admission["admission_sha256"] = paper_admission.calculate_paper_admission_sha256(admission)
    with pytest.raises(paper_admission.PaperAdmissionError):
        paper_admission.validate_paper_admission(admission)

    admission = valid_admission()
    admission["cycle_id"] = "contains whitespace"
    admission["admission_sha256"] = paper_admission.calculate_paper_admission_sha256(admission)
    with pytest.raises(paper_admission.PaperAdmissionError):
        paper_admission.validate_paper_admission(admission)


def test_tampered_admission_digest_is_rejected():
    admission = valid_admission()
    admission["admission_sha256"] = sha("f")

    with pytest.raises(paper_admission.PaperAdmissionError):
        paper_admission.validate_paper_admission(admission)
