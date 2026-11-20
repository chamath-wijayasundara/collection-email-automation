"""Map eligibility and customer responses to safe dry-run actions."""

from collection_email.domain.enums import (
    EligibilityStatus,
    IntendedAction,
    ResponseClassification,
)
from collection_email.domain.models import (
    DecisionResult,
    EligibilityResult,
    ResponseAssessment,
)
from collection_email.errors import MissingResponseAssessmentError

_REVIEW_DECISIONS = {
    ResponseClassification.NO_RESPONSE: (False, "no_response_ready_to_send", "No matched customer response; reminder may be simulated."),
    ResponseClassification.PAYMENT_PROMISED: (True, "payment_promised_requires_review", "Customer response requires human review."),
    ResponseClassification.PAYMENT_ALREADY_MADE: (True, "payment_already_made_requires_review", "Customer response requires human review."),
    ResponseClassification.PARTIAL_PAYMENT: (True, "partial_payment_requires_review", "Customer response requires human review."),
    ResponseClassification.DISPUTE_OR_QUERY: (True, "dispute_or_query_requires_review", "Customer response requires human review."),
    ResponseClassification.DOCUMENT_REQUEST: (True, "document_request_requires_review", "Customer response requires human review."),
    ResponseClassification.OUT_OF_OFFICE: (True, "out_of_office_requires_review", "Customer response requires human review."),
    ResponseClassification.BOUNCE_OR_INVALID_CONTACT: (True, "bounce_or_invalid_contact_requires_review", "Customer response requires human review."),
    ResponseClassification.NO_MEANINGFUL_RESPONSE: (True, "no_meaningful_response_requires_review", "Customer response requires human review."),
    ResponseClassification.AMBIGUOUS: (True, "ambiguous_response_requires_review", "Customer response requires human review."),
}


def decide(
    eligibility: EligibilityResult, response: ResponseAssessment | None
) -> DecisionResult:
    """Return the conservative decision prescribed by the response class."""
    if eligibility.status is not EligibilityStatus.ELIGIBLE:
        return DecisionResult(
            human_review_needed=False,
            reason_code="eligibility_ignored",
            reason="The invoice is not eligible for a reminder action.",
            intended_action=IntendedAction.IGNORED,
        )

    if response is None:
        raise MissingResponseAssessmentError(
            f"Eligible invoice {eligibility.document_number or eligibility.source_row} "
            "requires a response assessment."
        )

    review_needed, reason_code, reason = _REVIEW_DECISIONS[response.classification]
    return DecisionResult(
        human_review_needed=review_needed,
        reason_code=reason_code,
        reason=reason,
        intended_action=(
            IntendedAction.DRAFTED_FOR_REVIEW
            if review_needed
            else IntendedAction.READY_TO_SEND
        ),
    )
