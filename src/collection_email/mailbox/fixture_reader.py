"""Reader for locally stored synthetic mailbox fixtures."""

import json
from pathlib import Path

from pydantic import ValidationError

from collection_email.domain.models import (
    EmailMessage,
    MailboxLoadResult,
    ValidationIssue,
)
from collection_email.errors import FileValidationError


def load_mailbox_fixture(path: Path) -> MailboxLoadResult:
    """Load valid messages while reporting malformed entries independently."""
    try:
        payload = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as error:
        raise FileValidationError(f"Unable to read mailbox fixture: {path}") from error

    if not isinstance(payload, list):
        raise FileValidationError("Mailbox fixture must contain a JSON array")

    messages: list[EmailMessage] = []
    issues: list[ValidationIssue] = []
    for source_row, raw_message in enumerate(payload, start=1):
        try:
            messages.append(EmailMessage.model_validate(raw_message))
        except ValidationError as error:
            issues.append(
                ValidationIssue(
                    code="invalid_mailbox_fixture",
                    message=f"Invalid mailbox fixture entry: {error.errors()[0]['msg']}",
                    severity="error",
                    source_row=source_row,
                )
            )

    return MailboxLoadResult(messages=messages, issues=issues)
