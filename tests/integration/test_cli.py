"""Command-line contracts for the offline collection assessment workflow."""

from pathlib import Path

from typer.testing import CliRunner

from collection_email import cli
from collection_email.cli import app

PROJECT_ROOT = Path(__file__).parents[2]
WORKBOOK = PROJECT_ROOT / "docs" / "Collection Email Automation Synthetic Data.xlsx"
MAILBOX = PROJECT_ROOT / "fixtures" / "mailbox_responses.json"


def _run_args(tmp_path: Path) -> list[str]:
    return [
        "run",
        "--input",
        str(WORKBOOK),
        "--mailbox",
        str(MAILBOX),
        "--output",
        str(tmp_path / "outputs"),
        "--database",
        str(tmp_path / "audit.sqlite3"),
    ]


def _run_id(output: str) -> str:
    return next(line.removeprefix("Run ID: ") for line in output.splitlines() if line.startswith("Run ID: "))


def test_help_lists_each_assessor_command():
    """Removing a workflow command would leave the assessor without its entry point."""
    result = CliRunner().invoke(app, ["--help"])

    assert result.exit_code == 0
    assert "validate" in result.output
    assert "run" in result.output
    assert "inspect" in result.output


def test_validate_summarizes_records_without_creating_actions(tmp_path: Path):
    """Turning validation into a workflow run would create unintended dry-run artifacts."""
    database = tmp_path / "audit.sqlite3"
    output = tmp_path / "outputs"

    result = CliRunner().invoke(
        app,
        ["validate", "--input", str(WORKBOOK), "--as-of", "2026-03-25"],
    )

    assert result.exit_code == 0
    assert "Records: 300" in result.output
    assert "Valid records: 300" in result.output
    assert "Validation issues: 0" in result.output
    assert "Actions created: 0" in result.output
    assert not database.exists()
    assert not output.exists()


def test_validate_maps_file_level_failure_to_exit_two(tmp_path: Path):
    """Leaking a bad workbook as success would let callers process invalid inputs."""
    result = CliRunner().invoke(app, ["validate", "--input", str(tmp_path / "missing.xlsx")])

    assert result.exit_code == 2
    assert "ERROR:" in result.output
    assert "Unable to open workbook" in result.output


def test_as_of_requires_canonical_iso_date():
    """Accepting a non-ISO override would make boundary-date runs ambiguous."""
    result = CliRunner().invoke(
        app,
        ["validate", "--input", str(WORKBOOK), "--as-of", "25/03/2026"],
    )

    assert result.exit_code == 2
    assert "ISO date" in result.output


def test_run_reports_run_stage_action_and_output_counts(tmp_path: Path):
    """Dropping summary evidence would make an assessor inspect files manually."""
    result = CliRunner().invoke(app, _run_args(tmp_path))

    assert result.exit_code == 0
    assert "Run ID: " in result.output
    assert "Stage counts: initial=1, first=2, second=5, third=6, escalation=202" in result.output
    assert "Actions: 216" in result.output
    assert "Errors: 0" in result.output
    assert f"Output: {tmp_path / 'outputs'}" in result.output


def test_run_exits_one_when_an_eligible_invoice_has_a_processing_failure(
    tmp_path: Path, monkeypatch
):
    """Converting isolated processing failures to success would hide incomplete evidence."""
    from collection_email.application.run_service import RunService
    from collection_email.emails.renderer import EmailRenderer

    class FailingRenderer:
        def __init__(self) -> None:
            self._renderer = EmailRenderer(
                PROJECT_ROOT / "src" / "collection_email" / "emails" / "templates", "1.0"
            )

        def render(self, record, eligibility, response, decision):
            if record.document_number == "INV-2026-10010":
                raise RuntimeError("CLI test renderer failure")
            return self._renderer.render(record, eligibility, response, decision)

    class FailingRunService(RunService):
        def __init__(self, *, database_path=None):
            super().__init__(database_path=database_path, renderer=FailingRenderer())

    monkeypatch.setattr(cli, "RunService", FailingRunService)

    result = CliRunner().invoke(cli.app, _run_args(tmp_path))

    assert result.exit_code == 1
    assert "Errors: 1" in result.output


def test_inspect_prints_persisted_invoice_evidence_and_unknown_is_one(tmp_path: Path):
    """A missing audit trace must be distinguishable from a successfully inspected invoice."""
    runner = CliRunner()
    run = runner.invoke(app, _run_args(tmp_path))
    run_id = _run_id(run.output)
    database = tmp_path / "audit.sqlite3"

    inspected = runner.invoke(
        app,
        ["inspect", "--database", str(database), "--run-id", run_id, "--invoice", "INV-2026-10204"],
    )
    missing = runner.invoke(
        app,
        ["inspect", "--database", str(database), "--run-id", run_id, "--invoice", "INV-UNKNOWN"],
    )

    assert inspected.exit_code == 0
    assert "Normalized input:" in inspected.output
    assert "Eligibility:" in inspected.output
    assert "Match evidence:" in inspected.output
    assert "Response assessment:" in inspected.output
    assert "Review decision:" in inspected.output
    assert "Final dry-run action:" in inspected.output
    assert "artifact_path" in inspected.output
    assert missing.exit_code == 1
    assert "ERROR: No trace found" in missing.output
