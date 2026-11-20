"""Render stage-specific reminder email bodies without delivering them."""

from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, StrictUndefined

from collection_email.domain.models import (
    ARRecord,
    DecisionResult,
    EligibilityResult,
    RenderedEmail,
    ResponseAssessment,
)
from collection_email.errors import InvalidEmailHeaderError

_SUBJECTS = {
    "initial": "Initial reminder: invoice {document_number} is overdue",
    "first": "First reminder: invoice {document_number} is overdue",
    "second": "Second reminder: invoice {document_number} remains overdue",
    "third": "Third reminder: invoice {document_number} requires attention",
    "escalation": "Escalation: invoice {document_number} requires immediate attention",
}


class EmailRenderer:
    """Render local text and HTML templates for an eligible invoice."""

    def __init__(self, template_dir: Path, template_version: str) -> None:
        self.template_version = template_version
        loader = FileSystemLoader(str(template_dir))
        self._text_environment = Environment(
            loader=loader,
            autoescape=False,
            undefined=StrictUndefined,
            keep_trailing_newline=True,
        )
        self._html_environment = Environment(
            loader=loader,
            autoescape=True,
            undefined=StrictUndefined,
            keep_trailing_newline=True,
        )

    def render(
        self,
        record: ARRecord,
        eligibility: EligibilityResult,
        response: ResponseAssessment,
        decision: DecisionResult,
    ) -> RenderedEmail:
        """Return a rendered email artifact; this method never performs delivery."""
        stage = eligibility.stage.value
        _validate_header_value("document number", record.document_number)
        _validate_header_value("recipient address", record.customer_email)
        context = {
            "customer_name": record.customer_name,
            "document_number": record.document_number,
            "amount": _format_amount(record.amount),
            "due_date": record.due_date.isoformat(),
            "stage": stage,
            "response_context": response.summary if decision.human_review_needed else None,
        }
        subject = _SUBJECTS[stage].format(document_number=record.document_number)
        _validate_header_value("subject", subject)
        return RenderedEmail(
            to_address=record.customer_email,
            subject=subject,
            text_body=self._text_environment.get_template(f"{stage}.txt.j2").render(context),
            html_body=self._html_environment.get_template(f"{stage}.html.j2").render(context),
            stage=eligibility.stage,
            template_version=self.template_version,
        )


def _format_amount(amount: Decimal) -> str:
    return f"{amount.quantize(Decimal('0.01'), rounding=ROUND_HALF_UP):,.2f}"


def _validate_header_value(name: str, value: str) -> None:
    if "\r" in value or "\n" in value:
        raise InvalidEmailHeaderError(f"{name} must not contain CR or LF characters.")
