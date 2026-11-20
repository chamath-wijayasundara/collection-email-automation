"""Create offline-only, standards-compliant email artifacts."""

import os
import shutil
import tempfile
import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from email.message import EmailMessage
from email.policy import SMTP
from email.utils import format_datetime
from hashlib import sha256
from pathlib import Path

from collection_email.domain.enums import IntendedAction
from collection_email.domain.models import ActionResult, DecisionResult, RenderedEmail
from collection_email.errors import InvalidEmailHeaderError

OFFLINE_ASSESSMENT_SENDER = "collection-automation@offline.invalid"


class DryRunExecutor:
    """Write local ``.eml`` evidence; this class has no delivery capability."""

    def __init__(self, clock: Callable[[], datetime] | None = None) -> None:
        self._clock = clock or _now_utc

    def execute(
        self,
        rendered: RenderedEmail,
        decision: DecisionResult,
        destination: Path,
        run_id: str,
        document_number: str,
        idempotency_key: str,
        prior_action: ActionResult | None = None,
    ) -> ActionResult:
        """Create or copy an artifact below this run's output directory.

        A prior logical action is copied byte-for-byte.  The returned action keeps
        the prior action identity because the audit repository stores one logical
        action per idempotency key.
        """
        action_directory = self._action_directory(destination, decision.intended_action)
        filename = f"{self._safe_filename(document_number)}.eml"
        artifact_path = self._inside(action_directory / filename, destination)
        artifact_path.parent.mkdir(parents=True, exist_ok=True)

        if prior_action is not None:
            if prior_action.idempotency_key != idempotency_key:
                raise ValueError("A reused action must have the same idempotency key.")
            if prior_action.intended_action is not decision.intended_action:
                raise ValueError("A reused action must have the same intended action.")
            source = Path(prior_action.artifact_path)
            if not source.is_file():
                raise FileNotFoundError(f"Prior action artifact does not exist: {source}")
            self._atomic_copy(source, artifact_path)
            return ActionResult(
                action_id=prior_action.action_id,
                idempotency_key=idempotency_key,
                intended_action=decision.intended_action,
                artifact_path=artifact_path,
                action_reused=True,
                reused_from_action_id=prior_action.action_id,
            )

        message = self._message(rendered, run_id, document_number)
        self._atomic_write(artifact_path, message.as_bytes(policy=SMTP))
        return ActionResult(
            action_id=str(uuid.uuid4()),
            idempotency_key=idempotency_key,
            intended_action=decision.intended_action,
            artifact_path=artifact_path,
            action_reused=False,
        )

    @staticmethod
    def _action_directory(destination: Path, intended_action: IntendedAction) -> Path:
        if intended_action is IntendedAction.READY_TO_SEND:
            return Path(destination) / "emails" / "simulated_send"
        if intended_action is IntendedAction.DRAFTED_FOR_REVIEW:
            return Path(destination) / "emails" / "drafts"
        raise ValueError("Only ready_to_send and drafted_for_review actions create email artifacts.")

    def _message(self, rendered: RenderedEmail, run_id: str, document_number: str) -> EmailMessage:
        for name, value in {
            "recipient address": rendered.to_address,
            "subject": rendered.subject,
            "run ID": run_id,
            "invoice number": document_number,
        }.items():
            if "\r" in value or "\n" in value:
                raise InvalidEmailHeaderError(f"{name} must not contain CR or LF characters.")

        message = EmailMessage(policy=SMTP)
        message["From"] = OFFLINE_ASSESSMENT_SENDER
        message["To"] = rendered.to_address
        message["Subject"] = rendered.subject
        message["Date"] = _format_composition_date(self._clock())
        message["X-Collection-Automation-Mode"] = "DRY-RUN"
        message["X-Collection-Automation-Run-ID"] = run_id
        message["X-Collection-Automation-Invoice"] = document_number
        message.set_content(rendered.text_body, subtype="plain", charset="utf-8")
        message.add_alternative(rendered.html_body, subtype="html", charset="utf-8")
        boundary_evidence = (
            f"{rendered.to_address}\x00{rendered.subject}\x00{rendered.text_body}\x00"
            f"{rendered.html_body}\x00{run_id}\x00{document_number}"
        )
        message.set_boundary(f"=_collection_{sha256(boundary_evidence.encode('utf-8')).hexdigest()}")
        return message

    @staticmethod
    def _safe_filename(document_number: str) -> str:
        if (
            not document_number
            or document_number in {".", ".."}
            or ".." in document_number
            or any(character in document_number for character in ("/", "\\", "\x00", "\r", "\n"))
        ):
            raise ValueError("Document number is not safe for an artifact filename.")
        return document_number

    @staticmethod
    def _inside(path: Path, destination: Path) -> Path:
        root = Path(destination).resolve()
        resolved = path.resolve()
        try:
            resolved.relative_to(root)
        except ValueError as error:
            raise ValueError("Artifact path must stay inside the current run directory.") from error
        return resolved

    @staticmethod
    def _atomic_write(path: Path, content: bytes) -> None:
        temporary_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as temporary:
                temporary_path = Path(temporary.name)
                temporary.write(content)
                temporary.flush()
                os.fsync(temporary.fileno())
            os.replace(temporary_path, path)
        finally:
            if temporary_path is not None:
                temporary_path.unlink(missing_ok=True)

    @classmethod
    def _atomic_copy(cls, source: Path, destination: Path) -> None:
        with tempfile.NamedTemporaryFile(dir=destination.parent, delete=False) as temporary:
            temporary_path = Path(temporary.name)
        try:
            shutil.copyfile(source, temporary_path)
            with temporary_path.open("rb") as copied:
                os.fsync(copied.fileno())
            os.replace(temporary_path, destination)
        finally:
            temporary_path.unlink(missing_ok=True)


def _now_utc() -> datetime:
    return datetime.now(UTC)


def _format_composition_date(timestamp: datetime) -> str:
    """Format an actual timezone-aware composition time as an RFC 5322 UTC date."""
    if timestamp.tzinfo is None or timestamp.utcoffset() is None:
        raise ValueError("Dry-run composition clock must return a timezone-aware datetime.")
    return format_datetime(timestamp.astimezone(UTC))
