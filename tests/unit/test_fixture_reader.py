import json
from datetime import UTC, datetime

import pytest

from collection_email.errors import FileValidationError
from collection_email.mailbox.fixture_reader import load_mailbox_fixture


def test_malformed_fixture_entry_is_reported_without_dropping_valid_message(tmp_path):
    """Catches a reader that aborts the full mailbox on one invalid entry."""
    fixture_path = tmp_path / "mailbox.json"
    fixture_path.write_text(
        json.dumps(
            [
                {
                    "message_id": "valid-1",
                    "sender": "ar@example.test",
                    "recipients": ["collections@example.test"],
                    "subject": "INV-100",
                    "body": "We will pay.",
                    "received_at": "2026-03-25T09:00:00Z",
                },
                {
                    "message_id": "malformed-1",
                    "sender": "ar@example.test",
                },
            ]
        )
    )

    result = load_mailbox_fixture(fixture_path)

    assert [message.message_id for message in result.messages] == ["valid-1"]
    assert len(result.issues) == 1
    assert result.issues[0].code == "invalid_mailbox_fixture"
    assert result.issues[0].source_row == 2


def test_mailbox_timestamps_require_timezone_and_normalize_to_utc(tmp_path):
    """Naive fixture time must be isolated while aware time has one comparison basis."""
    fixture_path = tmp_path / "mailbox.json"
    fixture_path.write_text(
        json.dumps(
            [
                {
                    "message_id": "aware-1",
                    "sender": "ar@example.test",
                    "recipients": ["collections@example.test"],
                    "subject": "INV-100",
                    "body": "Aware timestamp",
                    "received_at": "2026-03-25T10:00:00+01:00",
                },
                {
                    "message_id": "naive-1",
                    "sender": "ar@example.test",
                    "recipients": ["collections@example.test"],
                    "subject": "INV-100",
                    "body": "Naive timestamp",
                    "received_at": "2026-03-25T09:00:00",
                },
            ]
        ),
        encoding="utf-8",
    )

    result = load_mailbox_fixture(fixture_path)

    assert [message.message_id for message in result.messages] == ["aware-1"]
    assert result.messages[0].received_at == datetime(2026, 3, 25, 9, tzinfo=UTC)
    assert result.messages[0].received_at.tzinfo is UTC
    assert [(issue.code, issue.source_row) for issue in result.issues] == [
        ("invalid_mailbox_fixture", 2)
    ]


def test_invalid_utf8_mailbox_is_a_file_validation_error(tmp_path):
    """Invalid fixture encoding is expected bad input, not a leaked decoder error."""
    fixture_path = tmp_path / "mailbox.json"
    fixture_path.write_bytes(b"[\xff]")

    with pytest.raises(FileValidationError, match="Unable to read mailbox fixture"):
        load_mailbox_fixture(fixture_path)
