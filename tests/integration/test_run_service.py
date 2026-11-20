"""End-to-end contracts for the collection workflow orchestrator."""

import sqlite3
from pathlib import Path

from collection_email.application.run_service import RunService
from collection_email.domain.models import RunRequest
from collection_email.emails.renderer import EmailRenderer

PROJECT_ROOT = Path(__file__).parents[2]
WORKBOOK = PROJECT_ROOT / "docs" / "Collection Email Automation Synthetic Data.xlsx"
MAILBOX = PROJECT_ROOT / "fixtures" / "mailbox_responses.json"


def _request(tmp_path: Path) -> RunRequest:
    return RunRequest(
        input_path=WORKBOOK,
        mailbox_path=MAILBOX,
        output_root=tmp_path / "outputs",
        database_path=tmp_path / "audit.sqlite3",
    )


def test_supplied_workbook_end_to_end(tmp_path: Path):
    """The supplied workbook produces every expected exact-day stage outcome."""
    service = RunService()

    result = service.run(_request(tmp_path))

    assert result.exit_code == 0
    assert {
        key: result.manifest["counts"][key]
        for key in ("initial", "first", "second", "third", "escalation")
    } == {
        "initial": 1,
        "first": 2,
        "second": 5,
        "third": 6,
        "escalation": 202,
    }
    assert len(result.traces) == 300

    eligible = [
        trace for trace in result.traces if trace["eligibility"]["status"] == "eligible"
    ]
    assert len(eligible) == 216
    assert all(trace["response"] is not None for trace in eligible)
    assert all(trace["decision"] is not None for trace in eligible)
    assert all(trace["action"] is not None for trace in eligible)
    assert all(Path(trace["action"]["artifact_path"]).is_file() for trace in eligible)

    ignored_reasons = [trace["eligibility"]["reason_code"] for trace in result.traces]
    assert ignored_reasons.count("non_invoice") == 5
    assert ignored_reasons.count("not_overdue") == 74
    assert ignored_reasons.count("no_stage_for_aging_day") == 5
    assert all(
        trace["response"] is None
        for trace in result.traces
        if trace["eligibility"]["status"] != "eligible"
    )


class _FailingRenderer:
    """Inject one unexpected rendering failure without changing other invoices."""

    def __init__(self) -> None:
        self._renderer = EmailRenderer(
            PROJECT_ROOT / "src" / "collection_email" / "emails" / "templates", "1.0"
        )

    def render(self, record, eligibility, response, decision):
        if record.document_number == "INV-2026-10010":
            raise RuntimeError("renderer fixture failure")
        return self._renderer.render(record, eligibility, response, decision)


def test_one_invoice_failure_does_not_abort_batch(tmp_path: Path):
    """An unexpected invoice failure becomes auditable while other actions continue."""
    service = RunService(renderer=_FailingRenderer())

    result = service.run(_request(tmp_path))
    failed = service.inspect(result.run_id, "INV-2026-10010")
    normal = service.inspect(result.run_id, "INV-2026-10001")

    assert result.exit_code == 1
    assert failed is not None
    assert failed["match"] is not None
    assert failed["response"] is not None
    assert failed["decision"]["intended_action"] == "failed"
    assert failed["error"]["code"] == "unexpected_processing_error"
    assert "renderer fixture failure" in failed["error"]["message"]
    assert "RuntimeError" in failed["error"]["debug_traceback"]
    assert normal is not None
    assert normal["action"] is not None
    assert Path(normal["action"]["artifact_path"]).is_file()
    assert len((result.output_dir / "errors.csv").read_text(encoding="utf-8").splitlines()) == 2


def test_run_creates_nested_database_and_output_parents(tmp_path: Path):
    """A clean documented destination must not require manual directory setup."""
    request = RunRequest(
        input_path=WORKBOOK,
        mailbox_path=MAILBOX,
        output_root=tmp_path / "nested" / "outputs" / "demo",
        database_path=tmp_path / "nested" / "outputs" / "collection_email.db",
    )

    result = RunService().run(request)

    assert result.exit_code == 0
    assert request.database_path.is_file()
    assert result.output_dir.is_dir()


def test_identical_rerun_reuses_logical_actions_and_copies_artifacts(tmp_path: Path):
    """A rerun has new evidence paths without creating duplicate logical actions."""
    request = _request(tmp_path)
    service = RunService()

    first = service.run(request)
    second = service.run(request)

    assert first.run_id != second.run_id
    assert first.exit_code == second.exit_code == 0
    first_actions = [trace["action"] for trace in first.traces if trace["action"]]
    second_actions = [trace["action"] for trace in second.traces if trace["action"]]
    assert len(first_actions) == len(second_actions) == 216
    assert all(action["action_reused"] is True for action in second_actions)
    assert all(action["reused_from_action_id"] == action["action_id"] for action in second_actions)
    assert all(
        Path(action["artifact_path"]).resolve().is_relative_to(second.output_dir.resolve())
        for action in second_actions
    )
    with sqlite3.connect(request.database_path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM actions").fetchone()[0] == 216
