"""Behavioural tests for SQLite-backed audit evidence."""

import sqlite3
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from collection_email.audit.repository import AuditRepository, compute_action_key
from collection_email.domain.enums import (
    EligibilityStatus,
    IntendedAction,
    ReminderStage,
    ResponseClassification,
)
from collection_email.domain.models import (
    ActionResult,
    ARRecord,
    DecisionResult,
    EligibilityResult,
    MatchResult,
    ResponseAssessment,
    RunRequest,
    RunResult,
)


def _request(database_path: Path) -> RunRequest:
    return RunRequest(
        input_path=Path("input.xlsx"),
        mailbox_path=Path("mailbox.json"),
        output_root=Path("outputs"),
        database_path=database_path,
        as_of_override=date(2026, 4, 1),
    )


def _record() -> ARRecord:
    return ARRecord(
        source_row=2,
        customer_name="Acme Ltd",
        customer_email="ar@acme.example.test",
        document_number="INV-100",
        amount=Decimal("123.45"),
        issue_date=date(2026, 2, 1),
        due_date=date(2026, 3, 1),
        source_aging_days=31,
        effective_aging_days=31,
        aging_bucket="31-60",
    )


def _eligibility() -> EligibilityResult:
    return EligibilityResult(
        source_row=2,
        document_number="INV-100",
        status=EligibilityStatus.ELIGIBLE,
        stage=ReminderStage.FIRST,
        reason_code="first_reminder_due",
        reason="First reminder is due.",
    )


def _response() -> ResponseAssessment:
    return ResponseAssessment(
        classification=ResponseClassification.PAYMENT_PROMISED,
        summary="Payment will be made Friday.",
        payment_date=date(2026, 4, 3),
        confidence=Decimal("0.90"),
        source_message_ids=["message-1"],
        match_reason="invoice_number",
    )


def _decision() -> DecisionResult:
    return DecisionResult(
        human_review_needed=True,
        reason_code="customer_response",
        reason="A response requires human review.",
        intended_action=IntendedAction.DRAFTED_FOR_REVIEW,
    )


def test_initialize_is_repeatable_and_round_trips_invoice_evidence(tmp_path: Path):
    """A missing schema or lossy evidence serialization would break this trace."""
    database_path = tmp_path / "audit.sqlite3"
    repository = AuditRepository(database_path)

    repository.initialize()
    repository.initialize()
    run_id = repository.start_run(_request(database_path))
    repository.record_invoice(
        run_id=run_id,
        record=_record(),
        eligibility=_eligibility(),
        match=MatchResult(
            messages=[],
            match_reason="invoice_number",
            strength="strong",
            unresolved_message_ids=[],
            requires_ambiguous_assessment=False,
        ),
        response=_response(),
        decision=_decision(),
    )
    repository.complete_run(
        run_id,
        RunResult(
            run_id=run_id,
            exit_code=0,
            manifest={"processed": 1},
            traces=[],
            validation_issues=[],
            errors=[],
            output_dir=tmp_path / "outputs" / run_id,
        ),
    )

    trace = repository.get_invoice_trace(run_id, "INV-100")

    assert trace == {
        "record": _record().model_dump(mode="json"),
        "eligibility": _eligibility().model_dump(mode="json"),
        "match": {
            "messages": [],
            "match_reason": "invoice_number",
            "strength": "strong",
            "unresolved_message_ids": [],
            "requires_ambiguous_assessment": False,
        },
        "response": _response().model_dump(mode="json"),
        "decision": _decision().model_dump(mode="json"),
        "action": None,
        "error": None,
    }


def test_unknown_run_or_invoice_returns_none(tmp_path: Path):
    """Returning a default trace would hide an audit lookup failure."""
    repository = AuditRepository(tmp_path / "audit.sqlite3")
    repository.initialize()

    assert repository.get_invoice_trace("missing-run", "INV-100") is None


def test_idempotency_key_is_stable_and_changes_with_logical_action_inputs():
    """Dropping any key input would incorrectly reuse a different action."""
    baseline = compute_action_key(
        "file-sha-256",
        date(2026, 4, 1),
        "INV-100",
        ReminderStage.FIRST,
        "v1",
    )

    assert baseline == compute_action_key(
        "file-sha-256",
        date(2026, 4, 1),
        "INV-100",
        ReminderStage.FIRST,
        "v1",
    )
    assert len(baseline) == 64
    assert baseline != compute_action_key(
        "file-sha-256", date(2026, 4, 2), "INV-100", ReminderStage.FIRST, "v1"
    )
    assert baseline != compute_action_key(
        "file-sha-256", date(2026, 4, 1), "INV-100", ReminderStage.SECOND, "v1"
    )
    assert baseline != compute_action_key(
        "file-sha-256", date(2026, 4, 1), "INV-100", ReminderStage.FIRST, "v2"
    )


def test_record_action_preserves_existing_logical_action(tmp_path: Path):
    """Replacing an existing key would lose the original simulated action."""
    database_path = tmp_path / "audit.sqlite3"
    repository = AuditRepository(database_path)
    repository.initialize()
    key = compute_action_key(
        "file-sha-256", date(2026, 4, 1), "INV-100", ReminderStage.FIRST, "v1"
    )
    first = ActionResult(
        action_id="action-1",
        idempotency_key=key,
        intended_action=IntendedAction.READY_TO_SEND,
        artifact_path=Path("emails/simulated_send/INV-100.eml"),
        action_reused=False,
    )

    repository.record_action(first)

    assert repository.find_action_by_key(key) == first
    with pytest.raises(sqlite3.IntegrityError):
        repository.record_action(
            first.model_copy(
                update={
                    "action_id": "action-2",
                    "artifact_path": Path("emails/replaced.eml"),
                }
            )
        )
    assert repository.find_action_by_key(key) == first
    with sqlite3.connect(database_path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM actions").fetchone()[0] == 1


def test_invoice_write_for_missing_run_rolls_back_and_enforces_foreign_keys(tmp_path: Path):
    """Disabling foreign keys could leave unattached invoice evidence behind."""
    repository = AuditRepository(tmp_path / "audit.sqlite3")
    repository.initialize()

    with pytest.raises(sqlite3.IntegrityError):
        repository.record_invoice(
            run_id="missing-run",
            record=_record(),
            eligibility=_eligibility(),
        )

    assert repository.get_invoice_trace("missing-run", "INV-100") is None
