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


def _create_legacy_database(database_path: Path) -> None:
    """Create the audit schema as it existed before trace action references."""
    with sqlite3.connect(database_path) as connection:
        connection.executescript(
            """
            CREATE TABLE runs (
                run_id TEXT PRIMARY KEY,
                request_json TEXT NOT NULL CHECK(json_valid(request_json)),
                started_at TEXT NOT NULL,
                completed_at TEXT,
                result_json TEXT CHECK(result_json IS NULL OR json_valid(result_json)),
                exit_code INTEGER
            );
            CREATE TABLE actions (
                action_id TEXT PRIMARY KEY,
                idempotency_key TEXT NOT NULL UNIQUE,
                intended_action TEXT NOT NULL,
                artifact_path TEXT NOT NULL,
                action_reused INTEGER NOT NULL CHECK(action_reused IN (0, 1)),
                reused_from_action_id TEXT REFERENCES actions(action_id)
            );
            CREATE TABLE invoice_traces (
                run_id TEXT NOT NULL REFERENCES runs(run_id),
                document_number TEXT NOT NULL,
                record_json TEXT CHECK(record_json IS NULL OR json_valid(record_json)),
                eligibility_json TEXT CHECK(eligibility_json IS NULL OR json_valid(eligibility_json)),
                match_json TEXT CHECK(match_json IS NULL OR json_valid(match_json)),
                response_json TEXT CHECK(response_json IS NULL OR json_valid(response_json)),
                decision_json TEXT CHECK(decision_json IS NULL OR json_valid(decision_json)),
                action_json TEXT CHECK(action_json IS NULL OR json_valid(action_json)),
                error_json TEXT CHECK(error_json IS NULL OR json_valid(error_json)),
                PRIMARY KEY (run_id, document_number)
            );
            """
        )
        connection.execute(
            "INSERT INTO runs (run_id, request_json, started_at) VALUES (?, ?, ?)",
            ("legacy-run", "{}", "2026-04-01T00:00:00+00:00"),
        )
        connection.execute(
            "INSERT INTO invoice_traces (run_id, document_number, record_json) VALUES (?, ?, ?)",
            ("legacy-run", "LEGACY-100", '{"source":"legacy"}'),
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
        "source_envelope": None,
    }


def test_unknown_run_or_invoice_returns_none(tmp_path: Path):
    """Returning a default trace would hide an audit lookup failure."""
    repository = AuditRepository(tmp_path / "audit.sqlite3")
    repository.initialize()

    assert repository.get_invoice_trace("missing-run", "INV-100") is None


def test_complete_run_rejects_mismatched_result_identity_without_mutating_run(tmp_path: Path):
    """Serializing a result under another run would make audit identities contradictory."""
    database_path = tmp_path / "audit.sqlite3"
    repository = AuditRepository(database_path)
    repository.initialize()
    run_id = repository.start_run(_request(database_path))
    mismatched_result = RunResult(
        run_id="different-run",
        exit_code=1,
        manifest={"processed": 0},
        traces=[],
        validation_issues=[],
        errors=[],
        output_dir=tmp_path / "outputs" / "different-run",
    )

    with pytest.raises(ValueError, match="does not match"):
        repository.complete_run(run_id, mismatched_result)

    with sqlite3.connect(database_path) as connection:
        assert connection.execute(
            "SELECT completed_at, result_json, exit_code FROM runs WHERE run_id = ?", (run_id,)
        ).fetchone() == (None, None, None)


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


def test_idempotency_key_includes_matched_mailbox_evidence_and_rendered_content():
    """A reply or draft change must create a new logical action, not copy stale evidence."""
    common = ("file-sha-256", date(2026, 4, 1), "INV-100", ReminderStage.FIRST, "v1")
    baseline = compute_action_key(
        *common,
        mailbox_evidence={"message_id": "reply-1", "body": "We will pay on 2026-04-03."},
        rendered_content={"subject": "Reminder", "text_body": "Pay by 2026-04-03."},
    )

    assert baseline == compute_action_key(
        *common,
        mailbox_evidence={"body": "We will pay on 2026-04-03.", "message_id": "reply-1"},
        rendered_content={"text_body": "Pay by 2026-04-03.", "subject": "Reminder"},
    )
    assert baseline != compute_action_key(
        *common,
        mailbox_evidence={"message_id": "reply-1", "body": "We will pay on 2026-04-04."},
        rendered_content={"subject": "Reminder", "text_body": "Pay by 2026-04-04."},
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


def test_invoice_trace_requires_a_persisted_action_and_round_trips_its_evidence(tmp_path: Path):
    """An unrecorded action ID would make a trace claim a fabricated action occurred."""
    database_path = tmp_path / "audit.sqlite3"
    repository = AuditRepository(database_path)
    repository.initialize()
    run_id = repository.start_run(_request(database_path))
    action = ActionResult(
        action_id="action-1",
        idempotency_key=compute_action_key(
            "file-sha-256", date(2026, 4, 1), "INV-100", ReminderStage.FIRST, "v1"
        ),
        intended_action=IntendedAction.READY_TO_SEND,
        artifact_path=Path("emails/simulated_send/INV-100.eml"),
        action_reused=False,
    )

    with pytest.raises(sqlite3.IntegrityError):
        repository.record_invoice(
            run_id=run_id,
            record=_record(),
            eligibility=_eligibility(),
            action=action,
        )

    assert repository.get_invoice_trace(run_id, "INV-100") is None
    repository.record_action(action)
    repository.record_invoice(
        run_id=run_id,
        record=_record(),
        eligibility=_eligibility(),
        action=action,
    )

    assert repository.get_invoice_trace(run_id, "INV-100")["action"] == action.model_dump(mode="json")


def test_initialize_migrates_legacy_traces_and_enforces_action_foreign_keys(tmp_path: Path):
    """A schema upgrade must retain old evidence while protecting future action traces."""
    database_path = tmp_path / "audit.sqlite3"
    _create_legacy_database(database_path)
    repository = AuditRepository(database_path)

    repository.initialize()
    repository.initialize()

    assert repository.get_invoice_trace("legacy-run", "LEGACY-100")["record"] == {"source": "legacy"}
    run_id = repository.start_run(_request(database_path))
    action = ActionResult(
        action_id="migrated-action",
        idempotency_key=compute_action_key(
            "file-sha-256", date(2026, 4, 1), "INV-100", ReminderStage.FIRST, "v1"
        ),
        intended_action=IntendedAction.READY_TO_SEND,
        artifact_path=Path("emails/simulated_send/INV-100.eml"),
        action_reused=False,
    )

    with pytest.raises(sqlite3.IntegrityError):
        repository.record_invoice(
            run_id=run_id,
            record=_record(),
            eligibility=_eligibility(),
            action=action,
        )

    repository.record_action(action)
    repository.record_invoice(
        run_id=run_id,
        record=_record(),
        eligibility=_eligibility(),
        action=action,
    )

    assert repository.get_invoice_trace(run_id, "INV-100")["action"] == action.model_dump(mode="json")


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


def test_trace_migrates_and_round_trips_invalid_source_envelope(tmp_path: Path):
    """Invalid rows need their raw fields and linked validation evidence after migration."""
    database_path = tmp_path / "audit.sqlite3"
    _create_legacy_database(database_path)
    repository = AuditRepository(database_path)
    repository.initialize()
    run_id = repository.start_run(_request(database_path))
    envelope = {
        "source_row": 7,
        "raw_values": {"Document Number": "INV-BAD", "Customer Email": "not-an-email"},
        "record": None,
        "issues": [
            {
                "code": "invalid_email",
                "message": "customer_email must be a valid email address",
                "severity": "error",
                "source_row": 7,
                "field": "customer_email",
            }
        ],
    }
    eligibility = EligibilityResult(
        source_row=7,
        document_number="INV-BAD",
        status=EligibilityStatus.INVALID,
        stage=ReminderStage.NONE,
        reason_code="invalid_source_row",
        reason="The source row has validation issues.",
    )

    repository.record_invoice(
        run_id,
        eligibility=eligibility,
        source_envelope=envelope,
    )

    trace = repository.get_invoice_trace(run_id, "INV-BAD")
    assert trace is not None
    assert trace["record"] is None
    assert trace["source_envelope"] == envelope
