"""Contract tests for offline email artifacts and assessor-facing outputs."""

import inspect
import json
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from collection_email.domain.enums import IntendedAction, ReminderStage
from collection_email.domain.models import (
    ActionResult,
    DecisionResult,
    RenderedEmail,
    RunResult,
)
from collection_email.emails.executor import DryRunExecutor
from collection_email.outputs.writer import OutputWriter


def _rendered() -> RenderedEmail:
    return RenderedEmail(
        to_address="ar@example.test",
        subject="Reminder: invoice INV-100 is overdue",
        text_body="Plain reminder body\n",
        html_body="<p>HTML reminder body</p>\n",
        stage=ReminderStage.FIRST,
        template_version="v1",
    )


def _decision(action: IntendedAction = IntendedAction.READY_TO_SEND) -> DecisionResult:
    return DecisionResult(
        human_review_needed=action is IntendedAction.DRAFTED_FOR_REVIEW,
        reason_code="test_decision",
        reason="A test decision.",
        intended_action=action,
    )


def test_executor_writes_only_dry_run_eml_inside_current_run(tmp_path: Path):
    """Dropping dry-run headers or writing elsewhere could misrepresent delivery."""
    run_dir = tmp_path / "run-a"
    action = DryRunExecutor().execute(
        _rendered(), _decision(), run_dir, "run-a", "INV-100", "key-100"
    )

    assert action.intended_action is IntendedAction.READY_TO_SEND
    assert action.artifact_path == run_dir / "emails" / "simulated_send" / "INV-100.eml"
    assert action.artifact_path.is_file()
    content = action.artifact_path.read_bytes()
    assert b"X-Collection-Automation-Mode: DRY-RUN" in content
    assert b"X-Collection-Automation-Run-ID: run-a" in content
    assert b"X-Collection-Automation-Invoice: INV-100" in content
    assert b"multipart/alternative" in content
    parameter_names = inspect.signature(DryRunExecutor.execute).parameters
    assert "send" not in parameter_names
    assert not {"smtp", "graph", "credential", "credentials"} & set(parameter_names)


def test_executor_uses_drafts_for_review_and_copies_prior_bytes_into_new_run(tmp_path: Path):
    """Pointing a reuse action at an old run would make the new run non-self-contained."""
    executor = DryRunExecutor()
    original = executor.execute(
        _rendered(),
        _decision(IntendedAction.DRAFTED_FOR_REVIEW),
        tmp_path / "first",
        "first",
        "INV-100",
        "key-100",
    )
    copied = executor.execute(
        _rendered(),
        _decision(IntendedAction.DRAFTED_FOR_REVIEW),
        tmp_path / "second",
        "second",
        "INV-100",
        "key-100",
        prior_action=original,
    )

    assert copied.action_reused is True
    assert copied.reused_from_action_id == original.action_id
    assert copied.artifact_path == tmp_path / "second" / "emails" / "drafts" / "INV-100.eml"
    assert copied.artifact_path.read_bytes() == original.artifact_path.read_bytes()
    assert copied.artifact_path.resolve().is_relative_to((tmp_path / "second").resolve())


def test_executor_rejects_a_prior_action_with_a_different_idempotency_key(tmp_path: Path):
    """Copying an unrelated prior artifact would make reuse evidence untruthful."""
    executor = DryRunExecutor()
    prior = executor.execute(
        _rendered(), _decision(), tmp_path / "first", "first", "INV-100", "original-key"
    )

    with pytest.raises(ValueError, match="idempotency key"):
        executor.execute(
            _rendered(), _decision(), tmp_path / "second", "second", "INV-100", "other-key", prior
        )


def test_executor_eml_bytes_are_deterministic_for_identical_evidence(tmp_path: Path):
    """A generated MIME boundary must not make identical dry-run evidence drift."""
    executor = DryRunExecutor()
    first = executor.execute(
        _rendered(), _decision(), tmp_path / "first", "same-run", "INV-100", "key-100"
    )
    second = executor.execute(
        _rendered(), _decision(), tmp_path / "second", "same-run", "INV-100", "key-100"
    )

    assert first.artifact_path.read_bytes() == second.artifact_path.read_bytes()


def test_output_writer_creates_complete_stable_contract_and_truthful_counts(tmp_path: Path):
    """Omitting empty evidence files or deriving counts from stale manifest values hides evidence."""
    output_dir = tmp_path / "run"
    artifact = output_dir / "emails" / "simulated_send" / "INV-100.eml"
    result = RunResult(
        run_id="run",
        exit_code=0,
        manifest={"run_id": "run", "counts": {"initial": 999}, "as_of_date": date(2026, 3, 25)},
        traces=[
            {
                "record": {"source_row": 2, "document_number": "INV-100"},
                "eligibility": {
                    "source_row": 2,
                    "document_number": "INV-100",
                    "status": "eligible",
                    "stage": "initial",
                    "reason_code": "initial_reminder_due",
                    "reason": "Initial reminder is due.",
                },
                "response": {
                    "classification": "no_response",
                    "summary": "No response.",
                    "payment_date": None,
                    "dispute_details": None,
                    "requested_action": None,
                    "confidence": Decimal("1.0"),
                    "source_message_ids": [],
                    "match_reason": "none",
                },
                "action": ActionResult(
                    action_id="action-100",
                    idempotency_key="key-100",
                    intended_action=IntendedAction.READY_TO_SEND,
                    artifact_path=artifact,
                    action_reused=False,
                ),
            }
        ],
        validation_issues=[],
        errors=[],
        output_dir=output_dir,
    )

    OutputWriter().write_run(output_dir, result)

    expected_files = {
        "manifest.json",
        "validation_issues.csv",
        "initial_reminders.csv",
        "first_reminders.csv",
        "second_reminders.csv",
        "third_reminders.csv",
        "escalations.csv",
        "ignored_records.csv",
        "response_assessments.csv",
        "actions.csv",
        "errors.csv",
    }
    assert expected_files <= {path.name for path in output_dir.iterdir()}
    assert (output_dir / "emails" / "simulated_send").is_dir()
    assert (output_dir / "emails" / "drafts").is_dir()
    assert (output_dir / "first_reminders.csv").read_text(encoding="utf-8").splitlines() == [
        "source_row,document_number,status,stage,reason_code,reason"
    ]
    manifest = json.loads((output_dir / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["counts"]["initial"] == 1
    assert manifest["counts"]["eligible"] == 1
    assert manifest["counts"]["traces"] == 1
    assert manifest["as_of_date"] == "2026-03-25"
