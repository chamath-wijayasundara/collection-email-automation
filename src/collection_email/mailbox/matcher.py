"""Conservative, deterministic associations between replies and invoices."""

import re
from collections.abc import Sequence

from collection_email.domain.models import ARRecord, EmailMessage, MatchResult


def _normalize_email(address: str) -> str:
    return address.strip().casefold()


def _contains_document(text: str, document_number: str) -> bool:
    escaped_document = re.escape(document_number.strip())
    return re.search(rf"(?<![A-Z0-9]){escaped_document}(?![A-Z0-9])", text, re.IGNORECASE) is not None


def _contains_any_invoice_reference(message: EmailMessage) -> bool:
    return re.search(r"\b(?:INV|CRD)-[A-Z0-9-]+\b", f"{message.subject}\n{message.body}", re.IGNORECASE) is not None


def _ordered(messages: Sequence[EmailMessage]) -> list[EmailMessage]:
    return sorted(messages, key=lambda message: message.received_at)


def _result(
    messages: Sequence[EmailMessage],
    match_reason: str,
    strength: str,
    *,
    unresolved_message_ids: Sequence[str] = (),
    requires_ambiguous_assessment: bool = False,
) -> MatchResult:
    return MatchResult(
        messages=_ordered(messages),
        match_reason=match_reason,
        strength=strength,
        unresolved_message_ids=list(unresolved_message_ids),
        requires_ambiguous_assessment=requires_ambiguous_assessment,
    )


def match_responses(
    record: ARRecord,
    customer_candidates: Sequence[ARRecord],
    messages: Sequence[EmailMessage],
    prior_thread_id: str | None = None,
) -> MatchResult:
    """Attach only reply evidence that has a defensible invoice-level link."""
    if prior_thread_id is not None:
        thread_messages = [message for message in messages if message.thread_id == prior_thread_id]
        if thread_messages:
            return _result(thread_messages, "prior_thread_id", "strong")

    customer_email = _normalize_email(record.customer_email)
    sender_matches = [message for message in messages if _normalize_email(message.sender) == customer_email]

    subject_matches = [
        message for message in sender_matches if _contains_document(message.subject, record.document_number)
    ]
    if subject_matches:
        return _result(subject_matches, "subject_document_and_sender", "strong")

    body_matches = [
        message
        for message in sender_matches
        if _contains_document(message.body, record.document_number)
    ]
    if body_matches:
        return _result(body_matches, "body_document_and_sender", "strong")

    document_matches = [
        message
        for message in messages
        if _contains_document(message.subject, record.document_number)
        or _contains_document(message.body, record.document_number)
    ]
    if document_matches:
        return _result(
            document_matches,
            "document_without_sender",
            "weak",
            requires_ambiguous_assessment=True,
        )

    sender_only_messages = [
        message for message in sender_matches if not _contains_any_invoice_reference(message)
    ]
    if sender_only_messages:
        matching_customer_candidates = [
            candidate
            for candidate in customer_candidates
            if _normalize_email(candidate.customer_email) == customer_email
        ]
        if len(matching_customer_candidates) == 1:
            return _result(
                sender_only_messages,
                "sender_only_single_candidate",
                "weak",
                requires_ambiguous_assessment=True,
            )
        if len(matching_customer_candidates) > 1:
            ordered_messages = _ordered(sender_only_messages)
            return _result(
                [],
                "sender_only_multiple_candidates",
                "none",
                unresolved_message_ids=[message.message_id for message in ordered_messages],
                requires_ambiguous_assessment=True,
            )

    return _result([], "no_defensible_match", "none")
