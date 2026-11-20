"""Apply the deterministic reminder eligibility rules."""

from collections.abc import Sequence

from collection_email.domain.enums import EligibilityStatus, ReminderStage
from collection_email.domain.models import EligibilityResult, RecordEnvelope


def _result(
    envelope: RecordEnvelope,
    *,
    status: EligibilityStatus,
    stage: ReminderStage,
    reason_code: str,
    reason: str,
) -> EligibilityResult:
    return EligibilityResult(
        source_row=envelope.source_row,
        document_number=_document_number(envelope),
        status=status,
        stage=stage,
        reason_code=reason_code,
        reason=reason,
    )


def _document_number(envelope: RecordEnvelope) -> str | None:
    """Retain a parseable raw identifier when validation prevents an AR record."""
    if envelope.record is not None:
        return envelope.record.document_number
    value = envelope.raw_values.get("Document Number")
    if not isinstance(value, str):
        return None
    normalized = value.strip()
    return normalized or None


def classify_eligibility(envelope: RecordEnvelope) -> EligibilityResult:
    """Classify one source row, preserving a result even when it is invalid."""
    if envelope.issues or envelope.record is None:
        return _result(
            envelope,
            status=EligibilityStatus.INVALID,
            stage=ReminderStage.NONE,
            reason_code="invalid_record",
            reason="The source row failed workbook validation.",
        )

    record = envelope.record
    if not record.document_number.startswith("INV-"):
        return _result(
            envelope,
            status=EligibilityStatus.IGNORED,
            stage=ReminderStage.NONE,
            reason_code="non_invoice",
            reason="The document is not an invoice.",
        )

    if record.effective_aging_days == 0:
        return _result(
            envelope,
            status=EligibilityStatus.IGNORED,
            stage=ReminderStage.NONE,
            reason_code="not_overdue",
            reason="The invoice is not overdue.",
        )

    stages = {
        4: (ReminderStage.INITIAL, "initial_reminder_due", "The initial reminder is due."),
        6: (ReminderStage.FIRST, "first_reminder_due", "The first reminder is due."),
        8: (ReminderStage.SECOND, "second_reminder_due", "The second reminder is due."),
        10: (ReminderStage.THIRD, "third_reminder_due", "The third reminder is due."),
    }
    stage = stages.get(record.effective_aging_days)
    if stage is not None:
        reminder_stage, reason_code, reason = stage
        return _result(
            envelope,
            status=EligibilityStatus.ELIGIBLE,
            stage=reminder_stage,
            reason_code=reason_code,
            reason=reason,
        )

    if record.effective_aging_days > 10:
        return _result(
            envelope,
            status=EligibilityStatus.ELIGIBLE,
            stage=ReminderStage.ESCALATION,
            reason_code="escalation_due",
            reason="The invoice is overdue beyond the defined reminder stages.",
        )

    return _result(
        envelope,
        status=EligibilityStatus.IGNORED,
        stage=ReminderStage.NONE,
        reason_code="no_stage_for_aging_day",
        reason="No reminder stage is defined for this aging day.",
    )


def classify_all(records: Sequence[RecordEnvelope]) -> list[EligibilityResult]:
    """Classify every source envelope in input order."""
    return [classify_eligibility(envelope) for envelope in records]
