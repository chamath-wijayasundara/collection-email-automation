"""Read and validate accounts-receivable workbooks."""

import hashlib
import re
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from openpyxl import load_workbook

from collection_email.domain.models import (
    ARRecord,
    RecordEnvelope,
    ValidationIssue,
    WorkbookReadResult,
)
from collection_email.errors import FileValidationError

SOURCE_HEADERS = (
    "Customer Name",
    "Customer Email",
    "Document Number",
    "Invoice Amount",
    "Issue Date",
    "Due Date",
    "Aging Days",
    "Aging Bucket",
)

FIELD_NAMES = (
    "customer_name",
    "customer_email",
    "document_number",
    "amount",
    "issue_date",
    "due_date",
    "aging_days",
    "aging_bucket",
)
EMAIL_PATTERN = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def fingerprint_file(path: Path) -> str:
    """Return the SHA-256 fingerprint of a workbook file."""
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _as_date(value: object) -> date | None:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return None


def _normalized_string(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = value.strip()
    return normalized or None


def _issue(code: str, message: str, source_row: int, field: str | None = None) -> ValidationIssue:
    return ValidationIssue(
        code=code,
        message=message,
        severity="error",
        source_row=source_row,
        field=field,
    )


def _as_decimal(value: object) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None


def _as_int(value: object) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    if number != number.to_integral_value():
        return None
    return int(number)


def read_workbook(path: Path, as_of_override: date | None = None) -> WorkbookReadResult:
    """Read the required workbook structure and control date."""
    formula_book = load_workbook(path, data_only=False)
    value_book = load_workbook(path, data_only=True)
    if "AR_Data" not in formula_book.sheetnames:
        raise FileValidationError("Workbook is missing required AR_Data sheet")
    if "Controls" not in formula_book.sheetnames:
        raise FileValidationError("Workbook is missing Controls!B2 As Of Date")

    headers = tuple(cell.value for cell in formula_book["AR_Data"][1])
    if headers != SOURCE_HEADERS:
        raise FileValidationError("AR_Data must contain the required source headers")

    source_as_of_date = _as_date(value_book["Controls"]["B2"].value)
    if source_as_of_date is None and as_of_override is None:
        raise FileValidationError("Controls!B2 As Of Date must be a valid date")
    effective_as_of_date = as_of_override or source_as_of_date
    assert effective_as_of_date is not None

    records: list[RecordEnvelope] = []
    normalized_document_numbers: dict[int, str | None] = {}
    for source_row, (formula_values, cached_values) in enumerate(
        zip(
            formula_book["AR_Data"].iter_rows(min_row=2, values_only=True),
            value_book["AR_Data"].iter_rows(min_row=2, values_only=True),
            strict=True,
        ),
        start=2,
    ):
        if all(value is None for value in formula_values):
            continue
        raw_values = dict(zip(SOURCE_HEADERS, formula_values, strict=True))
        values: dict[str, Any] = dict(zip(FIELD_NAMES, cached_values, strict=True))
        issues: list[ValidationIssue] = []

        normalized: dict[str, Any] = {}
        for field in ("customer_name", "customer_email", "document_number", "aging_bucket"):
            normalized[field] = _normalized_string(values[field])
            if normalized[field] is None:
                issues.append(_issue("required_value_missing", f"{field} is required", source_row, field))
        normalized_document_numbers[source_row] = normalized["document_number"]

        amount = _as_decimal(values["amount"])
        if amount is None:
            issues.append(_issue("required_value_missing", "amount is required", source_row, "amount"))
        elif amount < 0:
            issues.append(_issue("negative_amount", "amount must not be negative", source_row, "amount"))

        issue_date = _as_date(values["issue_date"])
        due_date = _as_date(values["due_date"])
        if issue_date is None:
            issues.append(_issue("required_value_missing", "issue_date is required", source_row, "issue_date"))
        if due_date is None:
            issues.append(_issue("required_value_missing", "due_date is required", source_row, "due_date"))
        if issue_date is not None and due_date is not None and issue_date > due_date:
            issues.append(_issue("issue_date_after_due_date", "issue_date must not be after due_date", source_row, "issue_date"))

        source_aging_days = _as_int(values["aging_days"])
        if source_aging_days is None:
            issues.append(_issue("required_value_missing", "aging_days is required", source_row, "aging_days"))
        elif source_aging_days < 0:
            issues.append(_issue("negative_aging_days", "aging_days must not be negative", source_row, "aging_days"))
        elif (
            source_as_of_date is not None
            and due_date is not None
            and source_aging_days != max((source_as_of_date - due_date).days, 0)
        ):
            issues.append(_issue("aging_days_mismatch", "aging_days does not match Controls!B2", source_row, "aging_days"))

        if normalized["customer_email"] is not None and not EMAIL_PATTERN.fullmatch(normalized["customer_email"]):
            issues.append(_issue("invalid_email", "customer_email must be a valid email address", source_row, "customer_email"))

        record = None
        if not issues:
            assert amount is not None
            assert issue_date is not None
            assert due_date is not None
            assert source_aging_days is not None
            record = ARRecord(
                source_row=source_row,
                customer_name=normalized["customer_name"],
                customer_email=normalized["customer_email"],
                document_number=normalized["document_number"],
                amount=amount,
                issue_date=issue_date,
                due_date=due_date,
                source_aging_days=source_aging_days,
                effective_aging_days=max((effective_as_of_date - due_date).days, 0),
                aging_bucket=normalized["aging_bucket"],
            )
        records.append(RecordEnvelope(source_row=source_row, raw_values=raw_values, record=record, issues=issues))

    document_rows: dict[str, list[RecordEnvelope]] = {}
    for envelope in records:
        document_number = normalized_document_numbers[envelope.source_row]
        if document_number is not None:
            document_rows.setdefault(document_number, []).append(envelope)
    for duplicate_rows in document_rows.values():
        if len(duplicate_rows) > 1:
            for envelope in duplicate_rows:
                envelope.issues.append(
                    _issue(
                        "duplicate_document_number",
                        "document_number occurs more than once",
                        envelope.source_row,
                        "document_number",
                    )
                )
                envelope.record = None

    issues = [issue for envelope in records for issue in envelope.issues]
    observations: list[ValidationIssue] = []
    if as_of_override is not None:
        for envelope in records:
            if envelope.record is not None and envelope.record.source_aging_days != envelope.record.effective_aging_days:
                observations.append(
                    ValidationIssue(
                        code="source_aging_differs_under_override",
                        message="source aging differs from aging calculated using the override date",
                        severity="observation",
                        source_row=envelope.source_row,
                        field="aging_days",
                    )
                )

    return WorkbookReadResult(
        file_fingerprint=fingerprint_file(path),
        source_as_of_date=source_as_of_date,
        effective_as_of_date=effective_as_of_date,
        as_of_source="override" if as_of_override else "workbook",
        source_row_count=len(records),
        records=records,
        valid_records=[envelope.record for envelope in records if envelope.record is not None],
        issues=issues,
        observations=observations,
    )
