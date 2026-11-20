"""Pydantic contracts shared by collection workflow modules."""

from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from collection_email.domain.enums import (
    EligibilityStatus,
    IntendedAction,
    ReminderStage,
    ResponseClassification,
)


class ARRecord(BaseModel):
    """A normalized row from the accounts-receivable workbook."""

    source_row: int
    customer_name: str
    customer_email: str
    document_number: str
    amount: Decimal
    issue_date: date
    due_date: date
    source_aging_days: int
    effective_aging_days: int
    aging_bucket: str


class ValidationIssue(BaseModel):
    """A validation or informational observation tied to an input row."""

    code: str
    message: str
    severity: str
    source_row: int | None = None
    field: str | None = None


class RecordEnvelope(BaseModel):
    """The raw row, its normalized record when valid, and its issues."""

    source_row: int
    raw_values: dict[str, Any]
    record: ARRecord | None = None
    issues: list[ValidationIssue] = Field(default_factory=list)


class WorkbookReadResult(BaseModel):
    """All evidence returned while reading an AR workbook."""

    file_fingerprint: str
    source_as_of_date: date | None
    effective_as_of_date: date
    as_of_source: str
    source_row_count: int
    records: list[RecordEnvelope]
    valid_records: list[ARRecord]
    issues: list[ValidationIssue]
    observations: list[ValidationIssue]


class EligibilityResult(BaseModel):
    """The reminder eligibility outcome for one workbook row."""

    source_row: int
    document_number: str | None = None
    status: EligibilityStatus
    stage: ReminderStage
    reason_code: str
    reason: str


class DeliveryStatusMetadata(BaseModel):
    """Validated linkage from a synthetic delivery-status notification."""

    model_config = ConfigDict(extra="forbid")

    original_message_id: str = Field(min_length=1)
    original_recipient: str = Field(min_length=1)
    document_number: str = Field(min_length=1)


class EmailMessage(BaseModel):
    """A normalized synthetic mailbox message."""

    message_id: str
    thread_id: str | None = None
    sender: str
    recipients: list[str]
    subject: str
    body: str
    received_at: datetime
    in_reply_to: str | None = None
    delivery_status: DeliveryStatusMetadata | None = None

    @field_validator("received_at")
    @classmethod
    def normalize_received_at(cls, value: datetime) -> datetime:
        """Require an unambiguous instant and use UTC for ordering and audit."""
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("received_at must be timezone-aware")
        return value.astimezone(UTC)


class MailboxLoadResult(BaseModel):
    """Mailbox fixtures loaded successfully with isolated issues."""

    messages: list[EmailMessage]
    issues: list[ValidationIssue]


class MatchResult(BaseModel):
    """The defensible mailbox evidence associated with an invoice."""

    messages: list[EmailMessage]
    match_reason: str
    strength: Literal["none", "weak", "strong"]
    unresolved_message_ids: list[str]
    requires_ambiguous_assessment: bool


class ResponseAssessment(BaseModel):
    """A deterministic customer-response assessment."""

    classification: ResponseClassification
    summary: str
    payment_date: date | None = None
    dispute_details: str | None = None
    requested_action: str | None = None
    confidence: Decimal
    source_message_ids: list[str]
    match_reason: str


class DecisionResult(BaseModel):
    """The review requirement and intended dry-run action for an invoice."""

    human_review_needed: bool
    reason_code: str
    reason: str
    intended_action: IntendedAction


class RenderedEmail(BaseModel):
    """A fully rendered email ready for dry-run artifact generation."""

    to_address: str
    subject: str
    text_body: str
    html_body: str
    stage: ReminderStage
    template_version: str


class ActionResult(BaseModel):
    """The persisted result of a simulated email action."""

    action_id: str
    idempotency_key: str
    intended_action: IntendedAction
    artifact_path: Path
    action_reused: bool
    reused_from_action_id: str | None = None


class RunRequest(BaseModel):
    """Inputs and destinations for one workflow execution."""

    input_path: Path
    mailbox_path: Path
    output_root: Path
    database_path: Path
    as_of_override: date | None = None


class RunResult(BaseModel):
    """The complete result and evidence of one workflow execution."""

    run_id: str
    exit_code: int
    manifest: dict[str, Any]
    traces: list[dict[str, Any]]
    validation_issues: list[ValidationIssue]
    errors: list[dict[str, Any]]
    output_dir: Path
