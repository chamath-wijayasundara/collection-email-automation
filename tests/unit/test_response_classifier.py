import json
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from collection_email.domain.models import EmailMessage, MatchResult
from collection_email.responses.classifier import RuleBasedResponseClassifier

VOCABULARY_PATH = (
    Path(__file__).parents[2] / "src/collection_email/responses/vocabulary.json"
)


def message(
    message_id: str, body: str, *, received_at: str = "2026-03-25T09:00:00Z"
) -> EmailMessage:
    return EmailMessage(
        message_id=message_id,
        sender="ar@acme.example.test",
        recipients=["collections@example.test"],
        subject="Re: INV-100",
        body=body,
        received_at=datetime.fromisoformat(received_at).astimezone(UTC),
    )


def matched(
    *messages: EmailMessage,
    strength: str = "strong",
    requires_ambiguous_assessment: bool = False,
) -> MatchResult:
    return MatchResult(
        messages=list(messages),
        match_reason="subject_document_and_sender",
        strength=strength,
        unresolved_message_ids=[],
        requires_ambiguous_assessment=requires_ambiguous_assessment,
    )


@pytest.fixture
def classifier() -> RuleBasedResponseClassifier:
    return RuleBasedResponseClassifier(VOCABULARY_PATH)


@pytest.mark.parametrize(
    ("body", "classification", "summary"),
    [
        (
            "Delivery failed because the recipient address is invalid.",
            "bounce_or_invalid_contact",
            "Delivery failure or invalid recipient reported.",
        ),
        (
            "I am out of office until 30 March 2026.",
            "out_of_office",
            "Out-of-office reply received.",
        ),
        (
            "We dispute the amount and need this investigated.",
            "dispute_or_query",
            "Customer reported a dispute or query.",
        ),
        (
            "Payment has already been made for this invoice.",
            "payment_already_made",
            "Customer reports payment has already been made.",
        ),
        (
            "We have paid part of the invoice; the balance will follow.",
            "partial_payment",
            "Customer reports a partial payment.",
        ),
        (
            "We will pay on 27 March 2026.",
            "payment_promised",
            "Customer promises payment on 2026-03-27.",
        ),
        (
            "Please send a copy of the invoice and purchase order.",
            "document_request",
            "Customer requested supporting documents.",
        ),
        (
            "Thanks for your email.",
            "no_meaningful_response",
            "No actionable customer response.",
        ),
    ],
)
def test_classification_uses_fixed_summary_templates(
    classifier, body, classification, summary
):
    assessment = classifier.assess(matched(message("message-1", body)))

    assert assessment.classification.value == classification
    assert assessment.summary == summary
    assert assessment.source_message_ids == ["message-1"]


def test_empty_match_is_no_response(classifier):
    assessment = classifier.assess(matched())

    assert assessment.classification.value == "no_response"
    assert assessment.summary == "No matched customer response."
    assert assessment.source_message_ids == []


def test_quoted_history_does_not_override_latest_explicit_status(classifier):
    assessment = classifier.assess(
        matched(
            message("earlier", "We dispute this invoice."),
            message(
                "latest",
                "We will pay on 27 March 2026.\n\n"
                "On 25 March 2026, Collections wrote:\n"
                "> We dispute this invoice.",
                received_at="2026-03-25T10:00:00Z",
            ),
        )
    )

    assert assessment.classification.value == "payment_promised"
    assert assessment.payment_date.isoformat() == "2026-03-27"
    assert assessment.source_message_ids == ["earlier", "latest"]


def test_latest_relevant_reply_controls_earlier_context(classifier):
    assessment = classifier.assess(
        matched(
            message("earlier", "We dispute this invoice."),
            message(
                "latest",
                "Payment has already been made.",
                received_at="2026-03-25T10:00:00Z",
            ),
        )
    )

    assert assessment.classification.value == "payment_already_made"
    assert assessment.source_message_ids == ["earlier", "latest"]


@pytest.mark.parametrize("payment_date", ["2026-03-27", "27/03/2026", "27 March 2026"])
def test_payment_date_formats_produce_the_same_date(classifier, payment_date):
    assessment = classifier.assess(
        matched(message("payment", f"We will pay on {payment_date}."))
    )

    assert assessment.classification.value == "payment_promised"
    assert assessment.payment_date.isoformat() == "2026-03-27"


@pytest.mark.parametrize("payment_date", ["tomorrow", "27 March", "2026-02-30"])
def test_unsafe_or_invalid_promised_payment_date_is_ambiguous(classifier, payment_date):
    assessment = classifier.assess(
        matched(message("payment", f"We will pay on {payment_date}."))
    )

    assert assessment.classification.value == "ambiguous"
    assert assessment.confidence < Decimal("0.80")
    assert assessment.payment_date is None


def test_conflicting_business_signals_are_ambiguous(classifier):
    assessment = classifier.assess(
        matched(
            message(
                "conflict", "We dispute the invoice, but we will pay on 27 March 2026."
            )
        )
    )

    assert assessment.classification.value == "ambiguous"
    assert assessment.confidence < Decimal("0.80")


def test_weak_match_is_ambiguous_even_when_its_text_looks_actionable(classifier):
    assessment = classifier.assess(
        matched(
            message("weak", "We will pay on 27 March 2026."),
            strength="weak",
            requires_ambiguous_assessment=True,
        )
    )

    assert assessment.classification.value == "ambiguous"
    assert assessment.confidence < Decimal("0.80")


def test_unresolved_source_message_ids_are_retained_on_ambiguous_assessment(classifier):
    assessment = classifier.assess(
        MatchResult(
            messages=[],
            match_reason="sender_only_multiple_candidates",
            strength="none",
            unresolved_message_ids=["unresolved-1", "unresolved-2"],
            requires_ambiguous_assessment=True,
        )
    )

    assert assessment.classification.value == "ambiguous"
    assert assessment.source_message_ids == ["unresolved-1", "unresolved-2"]


@pytest.mark.parametrize(
    ("case", "error"),
    [
        ("missing_version", "missing vocabulary key: version"),
        ("unsupported_version", "unsupported vocabulary version"),
        ("missing_phrase", "missing phrase classes: dispute_or_query"),
        ("phrase_not_list", "phrases.payment_promised must be a list of strings"),
        ("phrase_not_string", "phrases.payment_promised must be a list of strings"),
        ("missing_summary", "missing summary labels: ambiguous"),
        ("summary_not_string", "summary_labels.ambiguous must be a string"),
    ],
)
def test_classifier_rejects_valid_json_with_invalid_vocabulary_schema(
    tmp_path, case, error
):
    vocabulary = json.loads(VOCABULARY_PATH.read_text())
    if case == "missing_version":
        vocabulary.pop("version")
    elif case == "unsupported_version":
        vocabulary["version"] = "2.0"
    elif case == "missing_phrase":
        vocabulary["phrases"].pop("dispute_or_query")
    elif case == "phrase_not_list":
        vocabulary["phrases"]["payment_promised"] = "we will pay"
    elif case == "phrase_not_string":
        vocabulary["phrases"]["payment_promised"] = ["we will pay", 3]
    elif case == "missing_summary":
        vocabulary["summary_labels"].pop("ambiguous")
    else:
        vocabulary["summary_labels"]["ambiguous"] = 3
    invalid_path = tmp_path / f"invalid-{case}.json"
    invalid_path.write_text(json.dumps(vocabulary))

    with pytest.raises((ValueError, TypeError), match=error):
        RuleBasedResponseClassifier(invalid_path)


@pytest.mark.parametrize(
    ("section", "replacement", "error"),
    [
        ("phrases", [], "phrases must be an object"),
        ("summary_labels", [], "summary_labels must be an object"),
    ],
)
def test_classifier_rejects_valid_json_with_invalid_vocabulary_section_type(
    tmp_path, section, replacement, error
):
    vocabulary = json.loads(VOCABULARY_PATH.read_text())
    vocabulary[section] = replacement
    invalid_path = tmp_path / f"invalid-{section}.json"
    invalid_path.write_text(json.dumps(vocabulary))

    with pytest.raises((ValueError, TypeError), match=error):
        RuleBasedResponseClassifier(invalid_path)
