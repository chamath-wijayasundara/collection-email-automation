"""Contract tests for offline email artifacts and assessor-facing outputs."""

import csv
import inspect
import json
from datetime import UTC, date, datetime, timedelta, timezone
from decimal import Decimal
from email.parser import BytesParser
from email.policy import SMTP
from pathlib import Path
from typing import Self

import pytest

import collection_email.emails.executor as executor_module
import collection_email.outputs.writer as writer_module
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


def _composition_time() -> datetime:
    return datetime(2026, 3, 25, 12, 30, tzinfo=UTC)


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
    """A fixed composition clock must produce byte-identical new dry-run evidence."""
    executor = DryRunExecutor(clock=lambda: _composition_time())
    first = executor.execute(
        _rendered(), _decision(), tmp_path / "first", "same-run", "INV-100", "key-100"
    )
    second = executor.execute(
        _rendered(), _decision(), tmp_path / "second", "same-run", "INV-100", "key-100"
    )

    assert first.artifact_path.read_bytes() == second.artifact_path.read_bytes()


def test_executor_eml_has_deterministic_rfc_from_and_date_headers(tmp_path: Path):
    """An RFC-shaped artifact needs a truthful, parseable UTC composition date."""
    executor = DryRunExecutor(clock=lambda: _composition_time())
    action = executor.execute(
        _rendered(), _decision(), tmp_path / "run", "same-run", "INV-100", "key-100"
    )
    repeated = executor.execute(
        _rendered(), _decision(), tmp_path / "repeat", "same-run", "INV-100", "key-100"
    )
    parsed = BytesParser(policy=SMTP).parsebytes(action.artifact_path.read_bytes())

    assert parsed["From"] == "collection-automation@offline.invalid"
    assert parsed["Date"].datetime == _composition_time()
    assert repeated.artifact_path.read_bytes() == action.artifact_path.read_bytes()


def test_executor_normalizes_an_aware_clock_value_to_utc(tmp_path: Path):
    """A non-UTC but aware composition clock must retain the instant in the RFC date."""
    local_time = datetime(2026, 3, 25, 13, 30, tzinfo=timezone(timedelta(hours=1)))
    action = DryRunExecutor(clock=lambda: local_time).execute(
        _rendered(), _decision(), tmp_path / "run", "run", "INV-100", "key-100"
    )
    parsed = BytesParser(policy=SMTP).parsebytes(action.artifact_path.read_bytes())

    assert parsed["Date"].datetime == _composition_time()


def test_executor_rejects_naive_composition_clock_values(tmp_path: Path):
    """A timezone-naive clock could silently emit an incorrect RFC date."""
    with pytest.raises(ValueError, match="timezone-aware"):
        DryRunExecutor(clock=lambda: datetime(2026, 3, 25, 12, 30)).execute(
            _rendered(), _decision(), tmp_path / "run", "run", "INV-100", "key-100"
        )


@pytest.mark.parametrize("failure_point", ["fsync", "replace"])
def test_executor_removes_temporary_eml_when_atomic_finalization_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure_point: str
):
    """A failed email fsync or replacement must not leave partial evidence behind."""
    monkeypatch.setattr(executor_module.os, failure_point, _raise_atomic_failure)
    destination = tmp_path / "run"

    with pytest.raises(OSError, match="atomic failure"):
        DryRunExecutor().execute(
            _rendered(), _decision(), destination, "run", "INV-100", "key-100"
        )

    assert list((destination / "emails" / "simulated_send").iterdir()) == []


def test_executor_removes_temporary_eml_when_write_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """A failed email write must not leave partial evidence behind."""
    original_factory = executor_module.tempfile.NamedTemporaryFile
    monkeypatch.setattr(
        executor_module.tempfile,
        "NamedTemporaryFile",
        _failing_write_factory(original_factory),
    )
    destination = tmp_path / "run"

    with pytest.raises(OSError, match="write failure"):
        DryRunExecutor().execute(
            _rendered(), _decision(), destination, "run", "INV-100", "key-100"
        )

    assert list((destination / "emails" / "simulated_send").iterdir()) == []


@pytest.mark.parametrize(
    ("method", "suffix", "failure_point"),
    [
        ("_write_csv", ".csv", "fsync"),
        ("_write_csv", ".csv", "replace"),
        ("_write_json", ".json", "fsync"),
        ("_write_json", ".json", "replace"),
    ],
)
def test_output_atomic_writers_remove_temporary_files_when_replace_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, method: str, suffix: str, failure_point: str
):
    """A failed CSV or JSON fsync/replacement must not leave staged evidence behind."""
    destination = tmp_path / "output"
    destination.mkdir()
    monkeypatch.setattr(writer_module.os, failure_point, _raise_atomic_failure)

    with pytest.raises(OSError, match="atomic failure"):
        if method == "_write_csv":
            OutputWriter._write_csv(destination / f"evidence{suffix}", ["value"], [{"value": "x"}])
        else:
            OutputWriter._write_json(destination / f"evidence{suffix}", {"value": "x"})

    assert list(destination.iterdir()) == []


@pytest.mark.parametrize(("method", "suffix"), [("_write_csv", ".csv"), ("_write_json", ".json")])
def test_output_atomic_writers_remove_temporary_files_when_write_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, method: str, suffix: str
):
    """A failed CSV or JSON write must not leave staged evidence behind."""
    destination = tmp_path / "output"
    destination.mkdir()
    original_factory = writer_module.tempfile.NamedTemporaryFile
    monkeypatch.setattr(
        writer_module.tempfile,
        "NamedTemporaryFile",
        _failing_write_factory(original_factory),
    )

    with pytest.raises(OSError, match="write failure"):
        if method == "_write_csv":
            OutputWriter._write_csv(destination / f"evidence{suffix}", ["value"], [{"value": "x"}])
        else:
            OutputWriter._write_json(destination / f"evidence{suffix}", {"value": "x"})

    assert list(destination.iterdir()) == []


def _raise_atomic_failure(*_args: object) -> None:
    raise OSError("atomic failure")


def _failing_write_factory(original_factory: object):
    def factory(*args: object, **kwargs: object) -> _WriteFailingTemporaryFile:
        return _WriteFailingTemporaryFile(original_factory(*args, **kwargs))  # type: ignore[operator]

    return factory


class _WriteFailingTemporaryFile:
    def __init__(self, temporary: object) -> None:
        self._temporary = temporary

    @property
    def name(self) -> str:
        return self._temporary.name  # type: ignore[no-any-return, union-attr]

    def __enter__(self) -> Self:
        self._temporary.__enter__()  # type: ignore[union-attr]
        return self

    def __exit__(self, *args: object) -> object:
        return self._temporary.__exit__(*args)  # type: ignore[union-attr]

    def write(self, _content: object) -> None:
        raise OSError("write failure")

    def __getattr__(self, name: str) -> object:
        return getattr(self._temporary, name)


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


def test_csv_values_that_spreadsheet_apps_interpret_as_formulas_are_neutralized(tmp_path: Path):
    """Raw evidence must remain visible without becoming an executable CSV formula."""
    destination = tmp_path / "evidence.csv"

    OutputWriter._write_csv(
        destination,
        ["value"],
        [{"value": "=HYPERLINK(\"https://unsafe.example\")"}, {"value": "+1+1"}],
    )

    with destination.open(newline="", encoding="utf-8") as source:
        rows = list(csv.DictReader(source))
    assert rows[0]["value"].startswith("'=HYPERLINK")
    assert rows[1]["value"] == "'+1+1"
