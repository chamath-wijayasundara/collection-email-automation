"""Typer entry point for the offline collection-email assessment."""

from __future__ import annotations

import json
import sqlite3
from datetime import date
from pathlib import Path
from typing import Annotated

import typer

from collection_email.application.run_service import RunService
from collection_email.domain.models import RunRequest, WorkbookReadResult
from collection_email.errors import FileValidationError

app = typer.Typer(
    help="Run and inspect the offline collection-email assessment.",
    no_args_is_help=True,
    add_completion=False,
)


def _iso_date(value: str | None) -> date | None:
    """Parse only canonical ISO calendar dates for the explicit as-of override."""
    if value is None:
        return None
    try:
        parsed = date.fromisoformat(value)
    except ValueError as error:
        raise typer.BadParameter("must be an ISO date (YYYY-MM-DD)") from error
    if parsed.isoformat() != value:
        raise typer.BadParameter("must be an ISO date (YYYY-MM-DD)")
    return parsed


def _configuration_error(error: Exception) -> None:
    """Print an expected input or local-storage failure and return CLI code 2."""
    typer.echo(f"ERROR: {error}")
    raise typer.Exit(code=2)


def _print_validation(workbook: WorkbookReadResult) -> None:
    typer.echo("VALIDATE")
    typer.echo(f"Records: {workbook.source_row_count}")
    typer.echo(f"Valid records: {len(workbook.valid_records)}")
    typer.echo(f"Validation issues: {len(workbook.issues)}")
    typer.echo(f"Observations: {len(workbook.observations)}")
    typer.echo(f"As-of date: {workbook.effective_as_of_date.isoformat()}")
    typer.echo(f"As-of source: {workbook.as_of_source}")
    typer.echo("Actions created: 0")


@app.command()
def validate(
    input_path: Annotated[Path, typer.Option("--input", help="AR workbook to validate.")],
    as_of: Annotated[str | None, typer.Option("--as-of", help="ISO override date (YYYY-MM-DD).")] = None,
) -> None:
    """Validate workbook inputs without starting a run or generating actions."""
    as_of_override = _iso_date(as_of)
    try:
        workbook = RunService().validate(input_path, as_of_override)
    except FileValidationError as error:
        _configuration_error(error)
    _print_validation(workbook)


@app.command()
def run(
    input_path: Annotated[Path, typer.Option("--input", help="AR workbook to process.")],
    mailbox: Annotated[Path, typer.Option("--mailbox", help="Synthetic mailbox JSON fixture.")],
    output: Annotated[Path, typer.Option("--output", help="Directory for run evidence.")],
    database: Annotated[Path, typer.Option("--database", help="SQLite audit database path.")],
    as_of: Annotated[str | None, typer.Option("--as-of", help="ISO override date (YYYY-MM-DD).")] = None,
) -> None:
    """Execute the complete offline dry-run workflow."""
    as_of_override = _iso_date(as_of)
    request = RunRequest(
        input_path=input_path,
        mailbox_path=mailbox,
        output_root=output,
        database_path=database,
        as_of_override=as_of_override,
    )
    try:
        result = RunService(database_path=database).run(request)
    except (FileValidationError, OSError, sqlite3.Error) as error:
        _configuration_error(error)

    counts = result.manifest["counts"]
    typer.echo("RUN")
    typer.echo(f"Run ID: {result.run_id}")
    typer.echo(f"Records: {counts['traces']}")
    typer.echo(f"Eligible: {counts['eligible']}")
    typer.echo(
        "Stage counts: "
        f"initial={counts['initial']}, first={counts['first']}, "
        f"second={counts['second']}, third={counts['third']}, "
        f"escalation={counts['escalation']}"
    )
    typer.echo(f"Actions: {counts['actions']}")
    typer.echo(f"Errors: {counts['errors']}")
    typer.echo(f"Validation issues: {counts['validation_issues']}")
    typer.echo(f"Output: {result.output_dir}")
    raise typer.Exit(code=result.exit_code)


@app.command()
def inspect(
    run_id: Annotated[str, typer.Option("--run-id", help="Completed run identifier.")],
    invoice: Annotated[
        str,
        typer.Option("--invoice", help="Invoice number or source-row-N trace ID."),
    ],
    database: Annotated[Path, typer.Option("--database", help="SQLite audit database path.")],
) -> None:
    """Print persisted evidence for one invoice in one run."""
    try:
        trace = RunService(database_path=database).inspect(run_id, invoice)
    except (OSError, sqlite3.Error) as error:
        _configuration_error(error)
    if trace is None:
        typer.echo(f"ERROR: No trace found for invoice {invoice} in run {run_id}.")
        raise typer.Exit(code=1)

    typer.echo("INSPECT")
    typer.echo(f"Invoice: {invoice}")
    for label, key in (
        ("Normalized input", "record"),
        ("Source envelope", "source_envelope"),
        ("Eligibility", "eligibility"),
        ("Match evidence", "match"),
        ("Response assessment", "response"),
        ("Review decision", "decision"),
        ("Final dry-run action", "action"),
        ("Error", "error"),
    ):
        typer.echo(f"{label}: {json.dumps(trace[key], sort_keys=True, default=str)}")
