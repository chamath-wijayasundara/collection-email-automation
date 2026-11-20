"""Write stable, assessor-friendly evidence files for one run."""

import csv
import json
import os
import tempfile
from collections.abc import Mapping, Sequence
from datetime import date, datetime
from decimal import Decimal
from enum import Enum
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from collection_email.domain.models import RunResult

_ELIGIBILITY_HEADERS = [
    "source_row",
    "document_number",
    "status",
    "stage",
    "reason_code",
    "reason",
]
_IGNORED_HEADERS = [*_ELIGIBILITY_HEADERS, "raw_values", "validation_issue_codes"]
_VALIDATION_HEADERS = ["source_row", "field", "code", "severity", "message"]
_RESPONSE_HEADERS = [
    "source_row",
    "document_number",
    "classification",
    "summary",
    "payment_date",
    "dispute_details",
    "requested_action",
    "confidence",
    "source_message_ids",
    "match_reason",
]
_ACTION_HEADERS = [
    "source_row",
    "document_number",
    "action_id",
    "idempotency_key",
    "intended_action",
    "artifact_path",
    "action_reused",
    "reused_from_action_id",
]
_ERROR_HEADERS = ["source_row", "document_number", "code", "message"]
_STAGE_FILES = {
    "initial": "initial_reminders.csv",
    "first": "first_reminders.csv",
    "second": "second_reminders.csv",
    "third": "third_reminders.csv",
    "escalation": "escalations.csv",
}


class OutputWriter:
    """Persist deterministic CSV and JSON evidence without leaving the run directory."""

    def write_run(self, output_dir: Path, result: RunResult) -> None:
        """Write the complete output contract for ``result`` atomically per file."""
        root = Path(output_dir).resolve()
        root.mkdir(parents=True, exist_ok=True)
        self._inside(root / "emails" / "simulated_send", root).mkdir(parents=True, exist_ok=True)
        self._inside(root / "emails" / "drafts", root).mkdir(parents=True, exist_ok=True)

        traces = [_mapping(trace) for trace in result.traces]
        stage_rows = {stage: [] for stage in _STAGE_FILES}
        ignored_rows: list[dict[str, Any]] = []
        response_rows: list[dict[str, Any]] = []
        action_rows: list[dict[str, Any]] = []
        trace_errors: list[dict[str, Any]] = []

        for trace in traces:
            record = _mapping(trace.get("record"))
            eligibility = _mapping(trace.get("eligibility"))
            document_number = eligibility.get("document_number") or record.get("document_number")
            source_row = eligibility.get("source_row") or record.get("source_row")
            eligibility_row = {
                key: eligibility.get(key, "")
                for key in _ELIGIBILITY_HEADERS
            }
            eligibility_row["document_number"] = document_number or ""
            eligibility_row["source_row"] = source_row or ""
            stage = eligibility.get("stage")
            if eligibility.get("status") == "eligible" and stage in stage_rows:
                stage_rows[stage].append(eligibility_row)
            elif eligibility.get("status") in {"ignored", "invalid"}:
                envelope = _mapping(trace.get("source_envelope"))
                ignored_rows.append(
                    {
                        **eligibility_row,
                        "raw_values": envelope.get("raw_values", {}),
                        "validation_issue_codes": [
                            issue.get("code", "")
                            for issue in envelope.get("issues", [])
                            if isinstance(issue, Mapping)
                        ],
                    }
                )

            response = _mapping(trace.get("response"))
            if response:
                response_rows.append(
                    {
                        "source_row": source_row or "",
                        "document_number": document_number or "",
                        **{key: response.get(key, "") for key in _RESPONSE_HEADERS[2:]},
                    }
                )

            action = _mapping(trace.get("action"))
            if action:
                action_rows.append(
                    {
                        "source_row": source_row or "",
                        "document_number": document_number or "",
                        "action_id": action.get("action_id", ""),
                        "idempotency_key": action.get("idempotency_key", ""),
                        "intended_action": action.get("intended_action", ""),
                        "artifact_path": self._artifact_path(action.get("artifact_path"), root),
                        "action_reused": action.get("action_reused", ""),
                        "reused_from_action_id": action.get("reused_from_action_id", ""),
                    }
                )

            error = _mapping(trace.get("error"))
            if error:
                trace_errors.append(self._error_row(error, source_row, document_number))

        validation_rows = [self._validation_row(issue) for issue in result.validation_issues]
        errors = [self._error_row(error) for error in result.errors] + trace_errors

        self._write_csv(root / "validation_issues.csv", _VALIDATION_HEADERS, validation_rows)
        for stage, filename in _STAGE_FILES.items():
            self._write_csv(root / filename, _ELIGIBILITY_HEADERS, stage_rows[stage])
        self._write_csv(root / "ignored_records.csv", _IGNORED_HEADERS, ignored_rows)
        self._write_csv(root / "response_assessments.csv", _RESPONSE_HEADERS, response_rows)
        self._write_csv(root / "actions.csv", _ACTION_HEADERS, action_rows)
        self._write_csv(root / "errors.csv", _ERROR_HEADERS, errors)

        manifest = _json_safe(result.manifest)
        if not isinstance(manifest, dict):
            raise TypeError("Run manifest must be a JSON object.")
        manifest["run_id"] = result.run_id
        manifest["counts"] = self._counts(traces, validation_rows, errors, action_rows, response_rows)
        self._write_json(root / "manifest.json", manifest)

    @staticmethod
    def _counts(
        traces: Sequence[Mapping[str, Any]],
        validation_rows: Sequence[Mapping[str, Any]],
        errors: Sequence[Mapping[str, Any]],
        actions: Sequence[Mapping[str, Any]],
        responses: Sequence[Mapping[str, Any]],
    ) -> dict[str, int]:
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
            "validation_issues": len(validation_rows),
            "responses": len(responses),
            "actions": len(actions),
            "errors": len(errors),
        }
        for trace in traces:
            eligibility = _mapping(trace.get("eligibility"))
            status = eligibility.get("status")
            if status in {"eligible", "ignored", "invalid"}:
                counts[status] += 1
            stage = eligibility.get("stage")
            if status == "eligible" and stage in _STAGE_FILES:
                counts[stage] += 1
        return counts

    @staticmethod
    def _validation_row(issue: Any) -> dict[str, Any]:
        value = _mapping(issue)
        return {key: value.get(key, "") for key in _VALIDATION_HEADERS}

    @staticmethod
    def _error_row(
        error: Any, source_row: Any = "", document_number: Any = ""
    ) -> dict[str, Any]:
        value = _mapping(error)
        return {
            "source_row": value.get("source_row", source_row) or "",
            "document_number": value.get("document_number", document_number) or "",
            "code": value.get("code", value.get("type", "error")),
            "message": value.get("message", value.get("error", "")),
        }

    @staticmethod
    def _artifact_path(value: Any, root: Path) -> str:
        if not value:
            return ""
        path = Path(str(value))
        resolved = path.resolve() if path.is_absolute() else (root / path).resolve()
        try:
            return str(resolved.relative_to(root))
        except ValueError as error:
            raise ValueError("Action artifact paths must stay inside the current run directory.") from error

    @staticmethod
    def _inside(path: Path, root: Path) -> Path:
        resolved = path.resolve()
        try:
            resolved.relative_to(root)
        except ValueError as error:
            raise ValueError("Output path must stay inside the current run directory.") from error
        return resolved

    @classmethod
    def _write_csv(cls, path: Path, headers: list[str], rows: Sequence[Mapping[str, Any]]) -> None:
        temporary_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w", encoding="utf-8", newline="", dir=path.parent, delete=False
            ) as temporary:
                temporary_path = Path(temporary.name)
                writer = csv.DictWriter(temporary, fieldnames=headers, extrasaction="ignore")
                writer.writeheader()
                for row in rows:
                    writer.writerow({key: _csv_value(row.get(key, "")) for key in headers})
                temporary.flush()
                os.fsync(temporary.fileno())
            os.replace(temporary_path, path)
        finally:
            if temporary_path is not None:
                temporary_path.unlink(missing_ok=True)

    @staticmethod
    def _write_json(path: Path, value: Mapping[str, Any]) -> None:
        payload = json.dumps(
            _json_safe(value), ensure_ascii=False, allow_nan=False, separators=(",", ":"), sort_keys=True
        ).encode("utf-8")
        temporary_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as temporary:
                temporary_path = Path(temporary.name)
                temporary.write(payload)
                temporary.flush()
                os.fsync(temporary.fileno())
            os.replace(temporary_path, path)
        finally:
            if temporary_path is not None:
                temporary_path.unlink(missing_ok=True)


def _mapping(value: Any) -> dict[str, Any]:
    if value is None:
        return {}
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    if isinstance(value, Mapping):
        return dict(value)
    raise TypeError(f"Expected mapping-like trace evidence, received {type(value).__name__}.")


def _json_safe(value: Any) -> Any:
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    if isinstance(value, Mapping):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    return value


def _csv_value(value: Any) -> Any:
    if value is None:
        return ""
    if isinstance(value, (list, tuple, Mapping)):
        return json.dumps(_json_safe(value), ensure_ascii=False, separators=(",", ":"))
    normalized = _json_safe(value)
    if isinstance(normalized, str) and normalized.lstrip(" \t\r\n").startswith(("=", "+", "-", "@")):
        return f"'{normalized}"
    return normalized
