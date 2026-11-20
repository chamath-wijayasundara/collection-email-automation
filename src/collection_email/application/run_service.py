"""One-direction orchestration for the offline collection workflow."""

from __future__ import annotations

import traceback
from collections.abc import Callable
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

from collection_email.audit.repository import AuditRepository, compute_action_key
from collection_email.decisions.service import decide
from collection_email.domain.enums import EligibilityStatus, IntendedAction
from collection_email.domain.models import (
    ARRecord,
    DecisionResult,
    RunRequest,
    RunResult,
    WorkbookReadResult,
)
from collection_email.eligibility.service import classify_all
from collection_email.emails.executor import DryRunExecutor
from collection_email.emails.renderer import EmailRenderer
from collection_email.mailbox.fixture_reader import load_mailbox_fixture
from collection_email.mailbox.matcher import match_responses
from collection_email.outputs.writer import OutputWriter
from collection_email.responses.classifier import (
    ResponseClassifier,
    RuleBasedResponseClassifier,
)
from collection_email.workbook.reader import read_workbook


class RunService:
    """Coordinate domain services while retaining evidence for every source row."""

    def __init__(
        self,
        *,
        database_path: Path | None = None,
        renderer: EmailRenderer | None = None,
        classifier: ResponseClassifier | None = None,
        executor: DryRunExecutor | None = None,
        output_writer: OutputWriter | None = None,
        repository_factory: Callable[[Path], AuditRepository] = AuditRepository,
        template_version: str = "1.0",
    ) -> None:
        package_root = Path(__file__).parents[1]
        self._database_path = Path(database_path) if database_path is not None else None
        self._renderer = renderer or EmailRenderer(
            package_root / "emails" / "templates", template_version
        )
        self._classifier = classifier or RuleBasedResponseClassifier(
            package_root / "responses" / "vocabulary.json"
        )
        self._executor = executor or DryRunExecutor()
        self._output_writer = output_writer or OutputWriter()
        self._repository_factory = repository_factory
        self._repository: AuditRepository | None = None
        self._template_version = template_version

    def validate(
        self, input_path: Path, as_of_override: date | None = None
    ) -> WorkbookReadResult:
        """Read workbook evidence without creating runs, actions, or output files."""
        return read_workbook(Path(input_path), as_of_override=as_of_override)

    def inspect(self, run_id: str, document_number: str) -> dict[str, Any] | None:
        """Return persisted evidence for one row in a completed or active run."""
        if self._repository is None:
            if self._database_path is None:
                raise ValueError("RunService needs a database path before inspection.")
            self._repository = self._repository_factory(self._database_path)
        return self._repository.get_invoice_trace(run_id, document_number)

    def run(self, request: RunRequest) -> RunResult:
        """Run the complete dry-run workflow, isolating unexpected row failures."""
        workbook = self.validate(request.input_path, request.as_of_override)
        mailbox = load_mailbox_fixture(request.mailbox_path)

        Path(request.database_path).parent.mkdir(parents=True, exist_ok=True)
        Path(request.output_root).mkdir(parents=True, exist_ok=True)
        repository = self._repository_factory(request.database_path)
        repository.initialize()
        self._repository = repository
        self._database_path = request.database_path
        run_id = repository.start_run(request)
        output_dir = Path(request.output_root) / run_id
        eligibility_results = classify_all(workbook.records)
        eligible_records = [
            envelope.record
            for envelope, eligibility in zip(workbook.records, eligibility_results, strict=True)
            if eligibility.status is EligibilityStatus.ELIGIBLE and envelope.record is not None
        ]

        traces: list[dict[str, Any]] = []
        per_record_errors: list[dict[str, Any]] = []
        for envelope, eligibility in zip(workbook.records, eligibility_results, strict=True):
            trace = {
                "record": self._json_model(envelope.record),
                "eligibility": self._json_model(eligibility),
                "match": None,
                "response": None,
                "decision": None,
                "action": None,
                "error": None,
            }
            document_number = eligibility.document_number or f"source-row-{envelope.source_row}"

            if eligibility.status is not EligibilityStatus.ELIGIBLE:
                trace["decision"] = self._json_model(decide(eligibility, None))
                repository.record_invoice(
                    run_id,
                    record=envelope.record,
                    eligibility=eligibility,
                    decision=trace["decision"],
                    document_number=document_number,
                )
                traces.append(trace)
                continue

            assert envelope.record is not None
            try:
                self._process_eligible(
                    trace=trace,
                    record=envelope.record,
                    eligibility=eligibility,
                    eligible_records=eligible_records,
                    mailbox_messages=mailbox.messages,
                    file_fingerprint=workbook.file_fingerprint,
                    as_of_date=workbook.effective_as_of_date,
                    output_dir=output_dir,
                    run_id=run_id,
                    repository=repository,
                )
            except Exception as error:  # noqa: BLE001 - required per-record error boundary
                failed_decision = DecisionResult(
                    human_review_needed=False,
                    reason_code="unexpected_processing_error",
                    reason="The invoice could not be processed; review the recorded error.",
                    intended_action=IntendedAction.FAILED,
                )
                error_evidence = {
                    "source_row": envelope.source_row,
                    "document_number": document_number,
                    "code": "unexpected_processing_error",
                    "message": f"{type(error).__name__}: {error}",
                    "debug_traceback": traceback.format_exc(),
                }
                trace["decision"] = self._json_model(failed_decision)
                trace["error"] = error_evidence
                per_record_errors.append(error_evidence)
                repository.record_invoice(
                    run_id,
                    record=envelope.record,
                    eligibility=eligibility,
                    match=trace["match"],
                    response=trace["response"],
                    decision=trace["decision"],
                    error=error_evidence,
                    document_number=document_number,
                )
            traces.append(trace)

        validation_issues = [*workbook.issues, *workbook.observations, *mailbox.issues]
        manifest = self._manifest(
            workbook, request, traces, validation_issues, per_record_errors
        )
        result = RunResult(
            run_id=run_id,
            exit_code=1 if per_record_errors else 0,
            manifest=manifest,
            traces=traces,
            validation_issues=validation_issues,
            # Per-record errors are already present in their trace.  Keep this
            # aggregate channel for errors that are not attributable to a row.
            errors=[],
            output_dir=output_dir,
        )
        self._output_writer.write_run(output_dir, result)
        repository.complete_run(run_id, result)
        return result

    def _process_eligible(
        self,
        *,
        trace: dict[str, Any],
        record: ARRecord,
        eligibility: Any,
        eligible_records: list[ARRecord],
        mailbox_messages: list[Any],
        file_fingerprint: str,
        as_of_date: date,
        output_dir: Path,
        run_id: str,
        repository: AuditRepository,
    ) -> None:
        match = match_responses(record, eligible_records, mailbox_messages)
        trace["match"] = self._json_model(match)
        response = self._classifier.assess(match)
        trace["response"] = self._json_model(response)
        decision = decide(eligibility, response)
        trace["decision"] = self._json_model(decision)
        rendered = self._renderer.render(record, eligibility, response, decision)
        action_key = compute_action_key(
            file_fingerprint,
            as_of_date,
            record.document_number,
            eligibility.stage,
            rendered.template_version,
        )
        prior_action = repository.find_action_by_key(action_key)
        action = self._executor.execute(
            rendered,
            decision,
            output_dir,
            run_id,
            record.document_number,
            action_key,
            prior_action,
        )
        if prior_action is None:
            repository.record_action(action)

        trace["action"] = self._json_model(action)
        repository.record_invoice(
            run_id,
            record=record,
            eligibility=eligibility,
            match=trace["match"],
            response=trace["response"],
            decision=trace["decision"],
            action=trace["action"],
        )

    @staticmethod
    def _json_model(value: Any) -> Any:
        return None if value is None else value.model_dump(mode="json")

    def _manifest(
        self,
        workbook: WorkbookReadResult,
        request: RunRequest,
        traces: list[dict[str, Any]],
        validation_issues: list[Any],
        errors: list[dict[str, Any]],
    ) -> dict[str, Any]:
        counts = {
            "traces": len(traces),
            "eligible": 0,
            "ignored": 0,
            "invalid": 0,
            "initial": 0,
            "first": 0,
            "second": 0,
            "third": 0,
            "escalation": 0,
            "validation_issues": len(validation_issues),
            "responses": 0,
            "actions": 0,
            "errors": len(errors),
        }
        for trace in traces:
            eligibility = trace["eligibility"]
            status = eligibility["status"]
            counts[status] += 1
            if status == "eligible":
                counts[eligibility["stage"]] += 1
            counts["responses"] += trace["response"] is not None
            counts["actions"] += trace["action"] is not None
        return {
            "input_path": str(request.input_path),
            "mailbox_path": str(request.mailbox_path),
            "file_fingerprint": workbook.file_fingerprint,
            "as_of_date": workbook.effective_as_of_date.isoformat(),
            "as_of_source": workbook.as_of_source,
            "source_as_of_date": (
                workbook.source_as_of_date.isoformat() if workbook.source_as_of_date else None
            ),
            "template_version": self._template_version,
            "completed_at": datetime.now(UTC).isoformat(),
            "counts": counts,
        }
