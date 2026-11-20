import json

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
