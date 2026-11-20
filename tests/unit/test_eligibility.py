from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from collection_email.domain.models import ARRecord, RecordEnvelope, ValidationIssue
from collection_email.eligibility.service import classify_all, classify_eligibility
from collection_email.workbook.reader import read_workbook


def record_factory(*, age: int, document_number: str = "INV-100") -> ARRecord:
    return ARRecord(
        source_row=2,
        customer_name="Acme Ltd",
        customer_email="ar@acme.example.com",
        document_number=document_number,
        amount=Decimal("125.50"),
        issue_date=date(2026, 3, 1),
        due_date=date(2026, 3, 25),
        source_aging_days=age,
        effective_aging_days=age,
        aging_bucket="0-30",
    )


def envelope_factory(*, age: int, document_number: str = "INV-100") -> RecordEnvelope:
    record = record_factory(age=age, document_number=document_number)
    return RecordEnvelope(source_row=record.source_row, raw_values={}, record=record)


@pytest.mark.parametrize(
    ("age", "status", "stage", "reason_code"),
    [
        (0, "ignored", "none", "not_overdue"),
        (1, "ignored", "none", "no_stage_for_aging_day"),
        (4, "eligible", "initial", "initial_reminder_due"),
        (5, "ignored", "none", "no_stage_for_aging_day"),
        (6, "eligible", "first", "first_reminder_due"),
        (7, "ignored", "none", "no_stage_for_aging_day"),
        (8, "eligible", "second", "second_reminder_due"),
        (9, "ignored", "none", "no_stage_for_aging_day"),
        (10, "eligible", "third", "third_reminder_due"),
        (11, "eligible", "escalation", "escalation_due"),
    ],
)
def test_exact_assessment_boundaries(age, status, stage, reason_code):
    result = classify_eligibility(envelope_factory(age=age))

    assert (result.status.value, result.stage.value, result.reason_code) == (status, stage, reason_code)


def test_invalid_envelope_is_invalid_before_document_or_aging_rules():
    envelope = envelope_factory(age=4)
    envelope.issues.append(
        ValidationIssue(code="bad_row", message="invalid row", severity="error", source_row=envelope.source_row)
    )

    result = classify_eligibility(envelope)

    assert result.status.value == "invalid"
    assert result.stage.value == "none"
    assert result.reason_code == "invalid_record"
    assert result.document_number == "INV-100"


def test_missing_record_is_invalid():
    envelope = RecordEnvelope(source_row=8, raw_values={})

    result = classify_eligibility(envelope)

    assert result.status.value == "invalid"
    assert result.stage.value == "none"
    assert result.reason_code == "invalid_record"
    assert result.document_number is None


def test_non_invoice_is_ignored_before_aging_stage():
    result = classify_eligibility(envelope_factory(age=4, document_number="CRD-100"))

    assert (result.status.value, result.stage.value, result.reason_code) == (
        "ignored",
        "none",
        "non_invoice",
    )


def test_classify_all_returns_one_result_per_source_envelope():
    records = [envelope_factory(age=4), envelope_factory(age=0), envelope_factory(age=11)]
    records[1].source_row = 3
    records[2].source_row = 4

    results = classify_all(records)

    assert len(results) == len(records)
    assert [result.source_row for result in results] == [2, 3, 4]


def test_supplied_workbook_has_expected_eligibility_counts():
    workbook = read_workbook(Path("docs/Collection Email Automation Synthetic Data.xlsx"))
    results = classify_all(workbook.records)

    eligible = [result for result in results if result.status.value == "eligible"]
    stage_counts = {stage: sum(result.stage.value == stage for result in eligible) for stage in (
        "initial",
        "first",
        "second",
        "third",
        "escalation",
    )}
    assert stage_counts == {"initial": 1, "first": 2, "second": 5, "third": 6, "escalation": 202}

    invoice_results = [
        result
        for result, envelope in zip(results, workbook.records, strict=True)
        if envelope.record is not None and envelope.record.document_number.startswith("INV-")
    ]
    assert sum(result.reason_code == "no_stage_for_aging_day" for result in invoice_results) == 5
    assert sum(result.reason_code == "not_overdue" for result in invoice_results) == 74
