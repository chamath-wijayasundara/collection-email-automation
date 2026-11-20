"""Durable, deterministic SQLite storage for collection workflow evidence."""

import hashlib
import json
import sqlite3
import uuid
from collections.abc import Generator, Mapping
from contextlib import contextmanager
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from collection_email.domain.enums import ReminderStage
from collection_email.domain.models import ActionResult, RunRequest, RunResult


def _json_value(value: Any) -> Any:
    """Convert a model to JSON-safe values without changing its evidence."""
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    return value


def _canonical_json(value: Any) -> str:
    """Serialize audit evidence reproducibly for storage and comparison."""
    return json.dumps(
        _json_value(value),
        allow_nan=False,
        default=str,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def compute_action_key(
    file_fingerprint: str,
    as_of_date: date,
    document_number: str,
    stage: ReminderStage,
    template_version: str,
) -> str:
    """Return the stable SHA-256 identity of one logical simulated action."""
    payload = _canonical_json(
        [file_fingerprint, as_of_date.isoformat(), document_number, stage.value, template_version]
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


class AuditRepository:
    """Persist runs and per-invoice workflow evidence in a SQLite database."""

    def __init__(self, database_path: Path) -> None:
        self._database_path = Path(database_path)

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self._database_path, timeout=5.0, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 5000")
        return connection

    @contextmanager
    def _transaction(self) -> Generator[sqlite3.Connection, None, None]:
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            yield connection
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    def initialize(self) -> None:
        """Create the schema if needed; safely callable before every command."""
        statements = (
            """
            CREATE TABLE IF NOT EXISTS runs (
                run_id TEXT PRIMARY KEY,
                request_json TEXT NOT NULL CHECK(json_valid(request_json)),
                started_at TEXT NOT NULL,
                completed_at TEXT,
                result_json TEXT CHECK(result_json IS NULL OR json_valid(result_json)),
                exit_code INTEGER
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS actions (
                action_id TEXT PRIMARY KEY,
                idempotency_key TEXT NOT NULL UNIQUE,
                intended_action TEXT NOT NULL,
                artifact_path TEXT NOT NULL,
                action_reused INTEGER NOT NULL CHECK(action_reused IN (0, 1)),
                reused_from_action_id TEXT REFERENCES actions(action_id)
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS invoice_traces (
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
            )
            """,
        )
        with self._transaction() as connection:
            for statement in statements:
                connection.execute(statement)

    def start_run(self, request: RunRequest) -> str:
        """Start a run and return its opaque identifier."""
        run_id = str(uuid.uuid4())
        with self._transaction() as connection:
            connection.execute(
                "INSERT INTO runs (run_id, request_json, started_at) VALUES (?, ?, ?)",
                (run_id, _canonical_json(request), datetime.now(UTC).isoformat()),
            )
        return run_id

    def record_invoice(
        self,
        run_id: str,
        record: Any = None,
        eligibility: Any = None,
        match: Any = None,
        response: Any = None,
        decision: Any = None,
        action: Any = None,
        error: Any = None,
        *,
        document_number: str | None = None,
    ) -> None:
        """Append a single immutable invoice trace to an existing run."""
        document_number = document_number or self._document_number(record, eligibility)
        if not document_number:
            raise ValueError("An invoice trace needs a document number.")

        with self._transaction() as connection:
            connection.execute(
                """
                INSERT INTO invoice_traces (
                    run_id, document_number, record_json, eligibility_json, match_json,
                    response_json, decision_json, action_json, error_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    document_number,
                    self._json_or_none(record),
                    self._json_or_none(eligibility),
                    self._json_or_none(match),
                    self._json_or_none(response),
                    self._json_or_none(decision),
                    self._json_or_none(action),
                    self._json_or_none(error),
                ),
            )

    def complete_run(self, run_id: str, result: RunResult) -> None:
        """Persist the final aggregate result for an existing run."""
        with self._transaction() as connection:
            cursor = connection.execute(
                """
                UPDATE runs
                SET completed_at = ?, result_json = ?, exit_code = ?
                WHERE run_id = ? AND completed_at IS NULL
                """,
                (
                    datetime.now(UTC).isoformat(),
                    _canonical_json(result),
                    result.exit_code,
                    run_id,
                ),
            )
            if cursor.rowcount != 1:
                raise KeyError(f"Run {run_id!r} does not exist or is already complete.")

    def find_action_by_key(self, key: str) -> ActionResult | None:
        """Find the already-recorded logical action for an idempotency key."""
        connection = self._connect()
        try:
            row = connection.execute(
                """
                SELECT action_id, idempotency_key, intended_action, artifact_path,
                       action_reused, reused_from_action_id
                FROM actions
                WHERE idempotency_key = ?
                """,
                (key,),
            ).fetchone()
        finally:
            connection.close()
        if row is None:
            return None
        return ActionResult(
            action_id=row["action_id"],
            idempotency_key=row["idempotency_key"],
            intended_action=row["intended_action"],
            artifact_path=Path(row["artifact_path"]),
            action_reused=bool(row["action_reused"]),
            reused_from_action_id=row["reused_from_action_id"],
        )

    def record_action(self, action: ActionResult) -> None:
        """Record a new logical action without overwriting an existing one."""
        with self._transaction() as connection:
            connection.execute(
                """
                INSERT INTO actions (
                    action_id, idempotency_key, intended_action, artifact_path,
                    action_reused, reused_from_action_id
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    action.action_id,
                    action.idempotency_key,
                    action.intended_action.value,
                    str(action.artifact_path),
                    int(action.action_reused),
                    action.reused_from_action_id,
                ),
            )

    def get_invoice_trace(self, run_id: str, document_number: str) -> dict[str, Any] | None:
        """Return stored evidence for an invoice, or ``None`` if it was not recorded."""
        connection = self._connect()
        try:
            row = connection.execute(
                """
                SELECT record_json, eligibility_json, match_json, response_json,
                       decision_json, action_json, error_json
                FROM invoice_traces
                WHERE run_id = ? AND document_number = ?
                """,
                (run_id, document_number),
            ).fetchone()
        finally:
            connection.close()
        if row is None:
            return None
        return {
            "record": self._load_json(row["record_json"]),
            "eligibility": self._load_json(row["eligibility_json"]),
            "match": self._load_json(row["match_json"]),
            "response": self._load_json(row["response_json"]),
            "decision": self._load_json(row["decision_json"]),
            "action": self._load_json(row["action_json"]),
            "error": self._load_json(row["error_json"]),
        }

    @staticmethod
    def _json_or_none(value: Any) -> str | None:
        return None if value is None else _canonical_json(value)

    @staticmethod
    def _load_json(value: str | None) -> Any:
        return None if value is None else json.loads(value)

    @staticmethod
    def _document_number(record: Any, eligibility: Any) -> str | None:
        for value in (record, eligibility):
            if isinstance(value, Mapping):
                number = value.get("document_number")
            else:
                number = getattr(value, "document_number", None)
            if number:
                return str(number)
        return None
