from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from collection_email.domain.enums import (
    IntendedAction,
    ReminderStage,
    ResponseClassification,
)
from collection_email.domain.models import (
    ARRecord,
    DecisionResult,
    EligibilityResult,
    ResponseAssessment,
)
from collection_email.emails.renderer import EmailRenderer
from collection_email.errors import InvalidEmailHeaderError

TEMPLATE_DIR = Path(__file__).parents[2] / "src/collection_email/emails/templates"


def record() -> ARRecord:
    return ARRecord(
        source_row=2,
        customer_name="Acme & Sons <North>",
        customer_email="ar@acme.example.test",
        document_number="INV-100",
        amount=Decimal("1234.5"),
        issue_date=date(2026, 3, 1),
        due_date=date(2026, 3, 21),
        source_aging_days=4,
        effective_aging_days=4,
        aging_bucket="0-30",
    )


def eligibility(stage: ReminderStage) -> EligibilityResult:
    return EligibilityResult(
        source_row=2,
        document_number="INV-100",
        status="eligible",
        stage=stage,
        reason_code="fixture",
        reason="Fixture eligibility.",
    )


def response(summary: str = "Customer says <payment pending>.") -> ResponseAssessment:
    return ResponseAssessment(
        classification=ResponseClassification.PAYMENT_PROMISED,
        summary=summary,
        confidence=Decimal("0.90"),
        source_message_ids=["message-1"],
        match_reason="fixture",
    )


def decision(action: IntendedAction) -> DecisionResult:
    return DecisionResult(
        human_review_needed=action is IntendedAction.DRAFTED_FOR_REVIEW,
        reason_code="fixture",
        reason="Fixture decision.",
        intended_action=action,
    )


@pytest.mark.parametrize("stage", [stage for stage in ReminderStage if stage is not ReminderStage.NONE])
def test_every_stage_renders_required_invoice_details_in_text_and_html(stage):
    rendered = EmailRenderer(TEMPLATE_DIR, "test-v1").render(
        record(), eligibility(stage), response(), decision(IntendedAction.DRAFTED_FOR_REVIEW)
    )

    for body in (rendered.text_body, rendered.html_body):
        assert "INV-100" in body
        assert "1,234.50" in body
        assert "2026-03-21" in body
        assert stage.value in body.casefold()
    assert "Acme & Sons <North>" in rendered.text_body
    assert "Acme &amp; Sons &lt;North&gt;" in rendered.html_body
    assert rendered.to_address == "ar@acme.example.test"
    assert rendered.stage is stage
    assert rendered.template_version == "test-v1"


def test_escalation_is_distinct_and_firmer_than_a_reminder():
    renderer = EmailRenderer(TEMPLATE_DIR, "test-v1")
    first = renderer.render(
        record(), eligibility(ReminderStage.FIRST), response(), decision(IntendedAction.DRAFTED_FOR_REVIEW)
    )
    escalation = renderer.render(
        record(), eligibility(ReminderStage.ESCALATION), response(), decision(IntendedAction.DRAFTED_FOR_REVIEW)
    )

    assert escalation.subject != first.subject
    assert "escalation" in escalation.subject.casefold()
    assert "immediate attention" in escalation.text_body.casefold()


def test_response_summary_is_added_only_to_review_drafts():
    renderer = EmailRenderer(TEMPLATE_DIR, "test-v1")
    draft = renderer.render(
        record(), eligibility(ReminderStage.INITIAL), response(), decision(IntendedAction.DRAFTED_FOR_REVIEW)
    )
    simulated_send = renderer.render(
        record(), eligibility(ReminderStage.INITIAL), response(), decision(IntendedAction.READY_TO_SEND)
    )

    assert "Customer says <payment pending>." in draft.text_body
    assert "Customer says &lt;payment pending&gt;." in draft.html_body
    assert "Customer says <payment pending>." not in simulated_send.text_body
    assert "payment pending" not in simulated_send.html_body


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("document_number", "INV-100\r\nBcc: attacker@example.test"),
        ("customer_email", "ar@acme.example.test\nBcc: attacker@example.test"),
    ],
)
def test_header_bound_record_values_reject_crlf(field, value):
    unsafe_record = record().model_copy(update={field: value})

    with pytest.raises(InvalidEmailHeaderError, match="CR or LF"):
        EmailRenderer(TEMPLATE_DIR, "test-v1").render(
            unsafe_record,
            eligibility(ReminderStage.INITIAL),
            response(),
            decision(IntendedAction.READY_TO_SEND),
        )
