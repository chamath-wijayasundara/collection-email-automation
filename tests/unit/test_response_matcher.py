from datetime import UTC, date, datetime
from decimal import Decimal

from collection_email.domain.models import ARRecord, EmailMessage
from collection_email.mailbox.matcher import match_responses


def record(document_number="INV-100", customer_email="ar@acme.example.test"):
    return ARRecord(
        source_row=2,
        customer_name="Acme Ltd",
        customer_email=customer_email,
        document_number=document_number,
        amount=Decimal("100.00"),
        issue_date=date(2026, 3, 1),
        due_date=date(2026, 3, 21),
        source_aging_days=4,
        effective_aging_days=4,
        aging_bucket="0-30",
    )


def message(
    message_id,
    *,
    sender="ar@acme.example.test",
    subject="General question",
    body="Hello",
    thread_id=None,
    in_reply_to=None,
    delivery_status=None,
    received_at="2026-03-25T09:00:00Z",
):
    return EmailMessage(
        message_id=message_id,
        sender=sender,
        recipients=["collections@example.test"],
        subject=subject,
        body=body,
        thread_id=thread_id,
        in_reply_to=in_reply_to,
        delivery_status=delivery_status,
        received_at=datetime.fromisoformat(received_at).astimezone(UTC),
    )


def test_prior_thread_id_overrides_document_and_sender_matching():
    target = record()
    messages = [
        message("document-match", subject="RE: INV-100"),
        message("thread-later", thread_id="thread-100", received_at="2026-03-25T11:00:00Z"),
        message("thread-earlier", thread_id="thread-100", received_at="2026-03-25T10:00:00Z"),
    ]

    result = match_responses(target, [target], messages, prior_thread_id="thread-100")

    assert [item.message_id for item in result.messages] == ["thread-earlier", "thread-later"]
    assert result.match_reason == "prior_thread_id"
    assert result.strength == "strong"
    assert result.requires_ambiguous_assessment is False


def test_equal_received_times_are_ordered_by_message_id():
    target = record()
    messages = [
        message("thread-b", thread_id="thread-100"),
        message("thread-a", thread_id="thread-100"),
    ]

    result = match_responses(target, [target], messages, prior_thread_id="thread-100")

    assert [item.message_id for item in result.messages] == ["thread-a", "thread-b"]


def test_subject_document_match_requires_exact_normalized_sender():
    target = record()
    subject_match = message("subject-match", sender=" AR@ACME.EXAMPLE.TEST ", subject="RE: inv-100")

    result = match_responses(target, [target], [subject_match])

    assert [item.message_id for item in result.messages] == ["subject-match"]
    assert result.match_reason == "subject_document_and_sender"
    assert result.strength == "strong"


def test_body_document_match_follows_subject_priority():
    target = record()
    body_match = message("body-match", body="Could you confirm INV-100 is received?")

    result = match_responses(target, [target], [body_match])

    assert [item.message_id for item in result.messages] == ["body-match"]
    assert result.match_reason == "body_document_and_sender"
    assert result.strength == "strong"


def test_subject_and_later_body_matches_are_unioned_deduplicated_and_ordered():
    """Returning at the subject tier would hide a later reply that changes classification."""
    target = record()
    messages = [
        message(
            "later-body",
            subject="Re: account update",
            body="We will pay INV-100 on 27 March 2026.",
            thread_id="thread-b",
            received_at="2026-03-25T11:00:00Z",
        ),
        message(
            "both-earliest",
            subject="Re: INV-100",
            body="Question about INV-100",
            thread_id="thread-a",
            received_at="2026-03-25T08:00:00Z",
        ),
        message(
            "subject-middle",
            subject="Re: INV-100",
            body="We are checking this.",
            thread_id="thread-a",
            received_at="2026-03-25T09:00:00Z",
        ),
    ]

    result = match_responses(target, [target], messages)

    assert [item.message_id for item in result.messages] == [
        "both-earliest",
        "subject-middle",
        "later-body",
    ]
    assert result.match_reason == "subject_document_and_sender"
    assert result.strength == "strong"


def test_validated_delivery_status_metadata_defensibly_links_a_bounce():
    """A DSN may come from a daemon, but its validated original recipient links the invoice."""
    target = record()
    bounce = message(
        "bounce-1",
        sender="mailer-daemon@example.test",
        subject="Undeliverable: INV-100",
        body="Delivery failed because the recipient address is invalid.",
        in_reply_to="outgoing-INV-100",
        delivery_status={
            "original_message_id": "outgoing-INV-100",
            "original_recipient": "ar@acme.example.test",
            "document_number": "INV-100",
        },
    )

    result = match_responses(target, [target], [bounce])

    assert [item.message_id for item in result.messages] == ["bounce-1"]
    assert result.match_reason == "validated_delivery_status"
    assert result.strength == "strong"
    assert result.requires_ambiguous_assessment is False


def test_document_match_from_wrong_sender_is_ambiguous():
    target = record()
    wrong_sender = message("wrong-sender", sender="attacker@example.test", subject="INV-100")

    result = match_responses(target, [target], [wrong_sender])

    assert [item.message_id for item in result.messages] == ["wrong-sender"]
    assert result.match_reason == "document_without_sender"
    assert result.strength == "weak"
    assert result.requires_ambiguous_assessment is True


def test_sender_only_message_with_one_candidate_is_weak_and_ambiguous():
    target = record()
    sender_only = message("sender-only-1", body="I have a question.")

    single_candidate_match = match_responses(target, [target], [sender_only])

    assert [item.message_id for item in single_candidate_match.messages] == ["sender-only-1"]
    assert single_candidate_match.match_reason == "sender_only_single_candidate"
    assert single_candidate_match.strength == "weak"
    assert single_candidate_match.requires_ambiguous_assessment is True


def test_sender_only_message_with_multiple_candidates_stays_unresolved():
    target = record()
    another_candidate = record(document_number="INV-101")
    sender_only = message("sender-only-1", body="I have a question.")

    multi_candidate_match = match_responses(target, [target, another_candidate], [sender_only])

    assert multi_candidate_match.messages == []
    assert multi_candidate_match.match_reason == "sender_only_multiple_candidates"
    assert multi_candidate_match.strength == "none"
    assert multi_candidate_match.unresolved_message_ids == ["sender-only-1"]
    assert multi_candidate_match.requires_ambiguous_assessment is True


def test_message_without_thread_document_or_sender_has_no_defensible_match():
    target = record()
    unrelated = message("unrelated", sender="other@example.test", subject="Question", body="Can you help?")

    result = match_responses(target, [target], [unrelated])

    assert result.messages == []
    assert result.match_reason == "no_defensible_match"
    assert result.strength == "none"
    assert result.unresolved_message_ids == []
    assert result.requires_ambiguous_assessment is False
