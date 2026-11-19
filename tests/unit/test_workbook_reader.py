from datetime import date
from pathlib import Path

import pytest
from openpyxl import Workbook, load_workbook

from collection_email.errors import FileValidationError
from collection_email.workbook.reader import read_workbook

HEADERS = [
    "Customer Name",
    "Customer Email",
    "Document Number",
    "Invoice Amount",
    "Issue Date",
    "Due Date",
    "Aging Days",
    "Aging Bucket",
]


def write_workbook(tmp_path, *, include_ar_data=True, include_controls=True, as_of_date=date(2026, 3, 25)):
    workbook = Workbook()
    default_sheet = workbook.active
    workbook.remove(default_sheet)
    if include_ar_data:
        ar_data = workbook.create_sheet("AR_Data")
        ar_data.append(HEADERS)
    if include_controls:
        controls = workbook.create_sheet("Controls")
        controls.append(["Parameter", "Value"])
        controls.append(["As Of Date", as_of_date])
    path = tmp_path / "ar.xlsx"
    workbook.save(path)
    return path


def append_row(path, values):
    loaded = load_workbook(path)
    loaded["AR_Data"].append(values)
    loaded.save(path)


def valid_row(**changes):
    values = {
        "customer_name": "Acme Ltd",
        "customer_email": "ar@acme.example.com",
        "document_number": "INV-100",
        "amount": "125.50",
        "issue_date": date(2026, 3, 1),
        "due_date": date(2026, 3, 21),
        "aging_days": 4,
        "aging_bucket": "0-30",
    }
    values.update(changes)
    return [values[key] for key in values]


def test_missing_ar_data_sheet_is_a_file_validation_error(tmp_path):
    path = write_workbook(tmp_path, include_ar_data=False)

    with pytest.raises(FileValidationError, match="AR_Data"):
        read_workbook(path)


def test_missing_as_of_date_is_a_file_validation_error(tmp_path):
    path = write_workbook(tmp_path, include_controls=False)

    with pytest.raises(FileValidationError, match="As Of Date"):
        read_workbook(path)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("customer_name", "   "),
        ("customer_email", ""),
        ("document_number", None),
        ("amount", None),
        ("issue_date", None),
        ("due_date", None),
        ("aging_days", None),
        ("aging_bucket", None),
    ],
)
def test_required_blank_is_isolated_to_its_source_row(tmp_path, field, value):
    path = write_workbook(tmp_path)
    append_row(path, valid_row(**{field: value}))

    result = read_workbook(path)

    assert result.source_row_count == 1
    assert result.records[0].record is None
    assert result.records[0].issues[0].code == "required_value_missing"
    assert result.records[0].issues[0].field == field


def test_malformed_email_is_invalid(tmp_path):
    path = write_workbook(tmp_path)
    append_row(path, valid_row(customer_email="not-an-email"))

    result = read_workbook(path)

    assert result.valid_records == []
    assert result.issues[0].code == "invalid_email"


def test_negative_amount_is_invalid(tmp_path):
    path = write_workbook(tmp_path)
    append_row(path, valid_row(amount="-0.01"))

    result = read_workbook(path)

    assert result.records[0].issues[0].code == "negative_amount"


def test_issue_date_after_due_date_is_invalid(tmp_path):
    path = write_workbook(tmp_path)
    append_row(path, valid_row(issue_date=date(2026, 3, 22)))

    result = read_workbook(path)

    assert result.records[0].issues[0].code == "issue_date_after_due_date"


@pytest.mark.parametrize(
    ("aging_days", "expected_code"),
    [(-1, "negative_aging_days"), (3, "aging_days_mismatch")],
)
def test_invalid_source_aging_is_invalid(tmp_path, aging_days, expected_code):
    path = write_workbook(tmp_path)
    append_row(path, valid_row(aging_days=aging_days))

    result = read_workbook(path)

    assert result.records[0].issues[0].code == expected_code


def test_every_duplicate_document_number_is_invalid(tmp_path):
    path = write_workbook(tmp_path)
    append_row(path, valid_row(document_number=" INV-DUP "))
    append_row(path, valid_row(document_number="INV-DUP", customer_name="Other Ltd"))

    result = read_workbook(path)

    assert result.valid_records == []
    assert [record.issues[0].code for record in result.records] == [
        "duplicate_document_number",
        "duplicate_document_number",
    ]


def test_override_recomputes_effective_aging_without_invalidating_source_aging(tmp_path):
    path = write_workbook(tmp_path)
    append_row(path, valid_row())

    result = read_workbook(path, date(2026, 3, 27))

    assert result.valid_records[0].source_aging_days == 4
    assert result.valid_records[0].effective_aging_days == 6
    assert result.observations[0].code == "source_aging_differs_under_override"


def test_override_allows_missing_control_date_without_invalidating_source_aging(tmp_path):
    path = write_workbook(tmp_path, as_of_date=None)
    append_row(path, valid_row())

    result = read_workbook(path, date(2026, 3, 27))

    assert result.valid_records[0].source_aging_days == 4
    assert result.valid_records[0].effective_aging_days == 6


def test_supplied_workbook_has_expected_valid_source_rows():
    result = read_workbook(Path("docs/Collection Email Automation Synthetic Data.xlsx"))

    assert result.source_row_count == 300
    assert sum(record.document_number.startswith("INV-") for record in result.valid_records) == 295
    assert sum(record.document_number.startswith("CRD-") for record in result.valid_records) == 5
    assert result.issues == []
