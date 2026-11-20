from decimal import Decimal

import pytest

from collection_email.decisions.service import decide
from collection_email.domain.enums import (
    EligibilityStatus,
    ReminderStage,
    ResponseClassification,
)
from collection_email.domain.models import EligibilityResult, ResponseAssessment
from collection_email.errors import MissingResponseAssessmentError


def eligibility(*, status: EligibilityStatus = EligibilityStatus.ELIGIBLE) -> EligibilityResult:
    return EligibilityResult(
        source_row=2,
        document_number="INV-100",
        status=status,
        stage=ReminderStage.INITIAL if status is EligibilityStatus.ELIGIBLE else ReminderStage.NONE,
        reason_code="initial_reminder_due" if status is EligibilityStatus.ELIGIBLE else "not_overdue",
        reason="Fixture eligibility result.",
    )


def assessment(classification: ResponseClassification) -> ResponseAssessment:
    return ResponseAssessment(
        classification=classification,
        summary="Fixture response summary.",
        confidence=Decimal("0.90"),
        source_message_ids=[],
        match_reason="fixture",
    )


def test_no_response_is_ready_to_send_without_human_review():
    result = decide(eligibility(), assessment(ResponseClassification.NO_RESPONSE))

    assert (result.human_review_needed, result.intended_action.value) == (False, "ready_to_send")


@pytest.mark.parametrize(
    "classification",
    [
        value
        for value in ResponseClassification
        if value is not ResponseClassification.NO_RESPONSE
    ],
)
def test_every_customer_response_class_is_drafted_for_review(classification):
    result = decide(eligibility(), assessment(classification))

    assert (result.human_review_needed, result.intended_action.value) == (True, "drafted_for_review")


def test_ignored_eligibility_needs_no_response_assessment():
    result = decide(eligibility(status=EligibilityStatus.IGNORED), None)

    assert (result.human_review_needed, result.intended_action.value) == (False, "ignored")


def test_eligible_invoice_without_response_assessment_raises_domain_error():
    with pytest.raises(MissingResponseAssessmentError, match="INV-100"):
        decide(eligibility(), None)
