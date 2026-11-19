"""Enumerations used by the collection email workflow."""

from enum import StrEnum


class EligibilityStatus(StrEnum):
    """The eligibility outcome for an accounts-receivable record."""

    ELIGIBLE = "eligible"
    IGNORED = "ignored"
    INVALID = "invalid"


class ReminderStage(StrEnum):
    """The exact reminder stage selected by the eligibility rules."""

    INITIAL = "initial"
    FIRST = "first"
    SECOND = "second"
    THIRD = "third"
    ESCALATION = "escalation"
    NONE = "none"


class ResponseClassification(StrEnum):
    """The deterministic assessment of a matched customer response."""

    NO_RESPONSE = "no_response"
    PAYMENT_PROMISED = "payment_promised"
    PAYMENT_ALREADY_MADE = "payment_already_made"
    PARTIAL_PAYMENT = "partial_payment"
    DISPUTE_OR_QUERY = "dispute_or_query"
    DOCUMENT_REQUEST = "document_request"
    OUT_OF_OFFICE = "out_of_office"
    BOUNCE_OR_INVALID_CONTACT = "bounce_or_invalid_contact"
    NO_MEANINGFUL_RESPONSE = "no_meaningful_response"
    AMBIGUOUS = "ambiguous"


class IntendedAction(StrEnum):
    """The safe dry-run action selected for an invoice."""

    IGNORED = "ignored"
    READY_TO_SEND = "ready_to_send"
    DRAFTED_FOR_REVIEW = "drafted_for_review"
    FAILED = "failed"
