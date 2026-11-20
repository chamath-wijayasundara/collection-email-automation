"""Command-line contracts for the offline collection assessment workflow."""

import sqlite3
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile

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


def _create_pre_trace_id_database(database: Path) -> None:
    """Create the audit schema immediately before source trace IDs were added."""
    with sqlite3.connect(database) as connection:
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
                action_id TEXT REFERENCES actions(action_id),
                action_json TEXT CHECK(action_json IS NULL OR json_valid(action_json)),
                error_json TEXT CHECK(error_json IS NULL OR json_valid(error_json)),
                source_envelope_json TEXT CHECK(
                    source_envelope_json IS NULL OR json_valid(source_envelope_json)
                ),
                PRIMARY KEY (run_id, document_number)
            );
            INSERT INTO runs (run_id, request_json, started_at)
            VALUES ('legacy-run', '{}', '2026-04-01T00:00:00+00:00');
            INSERT INTO invoice_traces (
                run_id, document_number, record_json, source_envelope_json
            ) VALUES (
                'legacy-run', 'LEGACY-100', '{"source":"legacy"}', '{"source_row":2}'
            );
            """
        )


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


def test_validate_maps_malformed_ooxml_xml_to_exit_two(tmp_path: Path):
    """Malformed XML inside a ZIP workbook must be a concise input failure."""
    workbook = tmp_path / "malformed.xlsx"
    with ZipFile(WORKBOOK) as source_zip:
        members = {
            member.filename: source_zip.read(member.filename)
            for member in source_zip.infolist()
        }
    members["xl/worksheets/sheet1.xml"] = b"<worksheet><broken>"
    with ZipFile(workbook, "w", ZIP_DEFLATED) as destination_zip:
        for filename, content in members.items():
            destination_zip.writestr(filename, content)

    result = CliRunner().invoke(app, ["validate", "--input", str(workbook)])

    assert result.exit_code == 2
    assert "ERROR:" in result.output
    assert "Unable to parse workbook" in result.output


def test_validate_maps_missing_ooxml_content_types_part_to_exit_two(tmp_path: Path):
    """A ZIP missing its OOXML package manifest remains a concise file-level failure."""
    workbook = tmp_path / "missing-content-types.xlsx"
    with ZipFile(WORKBOOK) as source_zip, ZipFile(
        workbook, "w", ZIP_DEFLATED
    ) as destination_zip:
        for member in source_zip.infolist():
            if member.filename != "[Content_Types].xml":
                destination_zip.writestr(member, source_zip.read(member.filename))

    result = CliRunner().invoke(app, ["validate", "--input", str(workbook)])

    assert result.exit_code == 2
    assert "ERROR:" in result.output
    assert "required OOXML package parts" in result.output


def test_run_maps_invalid_utf8_mailbox_to_exit_two(tmp_path: Path):
    """A decoder failure in external fixture data belongs to CLI exit code two."""
    mailbox = tmp_path / "mailbox.json"
    mailbox.write_bytes(b"[\xff]")
    args = _run_args(tmp_path)
    args[args.index(str(MAILBOX))] = str(mailbox)

    result = CliRunner().invoke(app, args)

    assert result.exit_code == 2
    assert "ERROR:" in result.output
    assert "Unable to read mailbox fixture" in result.output


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
    assert "Source envelope:" in inspected.output
    assert "Eligibility:" in inspected.output
    assert "Match evidence:" in inspected.output
    assert "Response assessment:" in inspected.output
    assert "Review decision:" in inspected.output
    assert "Final dry-run action:" in inspected.output
    assert "artifact_path" in inspected.output
    assert missing.exit_code == 1
    assert "ERROR: No trace found" in missing.output


def test_inspect_migrates_pre_trace_id_database_before_any_new_run(tmp_path: Path):
    """Existing audit evidence must remain inspectable before another run upgrades it."""
    database = tmp_path / "legacy.sqlite3"
    _create_pre_trace_id_database(database)

    result = CliRunner().invoke(
        app,
        [
            "inspect",
            "--database",
            str(database),
            "--run-id",
            "legacy-run",
            "--invoice",
            "LEGACY-100",
        ],
    )

    assert result.exit_code == 0
    assert 'Normalized input: {"source": "legacy"}' in result.output
    assert 'Source envelope: {"source_row": 2}' in result.output
    with sqlite3.connect(database) as connection:
        columns = {row[1] for row in connection.execute("PRAGMA table_info(invoice_traces)")}
    assert "trace_id" in columns


def test_inspect_missing_database_is_an_error_without_creating_a_file(tmp_path: Path):
    """Inspecting cannot silently turn a path typo into an empty audit database."""
    database = tmp_path / "missing.sqlite3"

    result = CliRunner().invoke(
        app,
        [
            "inspect",
            "--database",
            str(database),
            "--run-id",
            "missing-run",
            "--invoice",
            "MISSING-100",
        ],
    )

    assert result.exit_code == 2
    assert "ERROR:" in result.output
    assert not database.exists()
