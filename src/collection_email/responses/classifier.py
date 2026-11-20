"""Offline, conservative classification of matched customer replies."""

import json
import re
from collections.abc import Mapping
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Protocol

from collection_email.domain.enums import ResponseClassification
from collection_email.domain.models import EmailMessage, MatchResult, ResponseAssessment

_PRECEDENCE = (
    ResponseClassification.BOUNCE_OR_INVALID_CONTACT,
    ResponseClassification.OUT_OF_OFFICE,
    ResponseClassification.DISPUTE_OR_QUERY,
    ResponseClassification.PAYMENT_ALREADY_MADE,
    ResponseClassification.PARTIAL_PAYMENT,
    ResponseClassification.PAYMENT_PROMISED,
    ResponseClassification.DOCUMENT_REQUEST,
)
_SUPPORTED_VOCABULARY_VERSIONS = {"1.0"}
_REQUIRED_SUMMARY_LABELS = {classification.value for classification in ResponseClassification}
_QUOTE_MARKER = re.compile(r"^\s*on .+ wrote:\s*$", re.IGNORECASE)
_PUNCTUATION = re.compile(r"[^\w\s]")
_ISO_DATE = re.compile(r"\b(\d{4})-(\d{2})-(\d{2})\b")
_UK_DATE = re.compile(r"\b(\d{1,2})/(\d{1,2})/(\d{4})\b")
_WRITTEN_DATE = re.compile(
    r"\b(\d{1,2})\s+(january|february|march|april|may|june|july|august|september|october|november|december)\s+(\d{4})\b",
    re.IGNORECASE,
)
_UNSAFE_DATE = re.compile(
    r"\b(tomorrow|today|next\s+(?:week|month|monday|tuesday|wednesday|thursday|friday|saturday|sunday))\b"
    r"|\b\d{1,2}\s+(?:january|february|march|april|may|june|july|august|september|october|november|december)\b(?!\s+\d{4})",
    re.IGNORECASE,
)


class ResponseClassifier(Protocol):
    """The boundary for deterministic or future model-backed classifiers."""

    def assess(self, match: MatchResult) -> ResponseAssessment:
        """Assess defensibly matched customer response evidence."""


def _unquoted_text(text: str) -> str:
    kept: list[str] = []
    for line in text.splitlines():
        if _QUOTE_MARKER.match(line):
            break
        if not line.lstrip().startswith(">"):
            kept.append(line)
    return "\n".join(kept).strip()


def _normalized(text: str) -> str:
    return " ".join(_PUNCTUATION.sub(" ", text.casefold()).split())


def _parse_date(text: str) -> date | None:
    for pattern, constructor in (
        (
            _ISO_DATE,
            lambda match: date(
                int(match.group(1)), int(match.group(2)), int(match.group(3))
            ),
        ),
        (
            _UK_DATE,
            lambda match: date(
                int(match.group(3)), int(match.group(2)), int(match.group(1))
            ),
        ),
        (
            _WRITTEN_DATE,
            lambda match: date.fromisoformat(
                f"{match.group(3)}-{_month_number(match.group(2)):02d}-{int(match.group(1)):02d}"
            ),
        ),
    ):
        found = pattern.search(text)
        if found is not None:
            try:
                return constructor(found)
            except ValueError:
                return None
    return None


def _month_number(name: str) -> int:
    return [
        "january",
        "february",
        "march",
        "april",
        "may",
        "june",
        "july",
        "august",
        "september",
        "october",
        "november",
        "december",
    ].index(name.casefold()) + 1


class RuleBasedResponseClassifier:
    """Classify fixture messages using a versioned local vocabulary."""

    def __init__(self, vocabulary_path: Path):
        vocabulary = json.loads(vocabulary_path.read_text())
        self.version, self._phrases, self._summaries = _validate_vocabulary(vocabulary)

    def assess(self, match: MatchResult) -> ResponseAssessment:
        """Return one conservative deterministic assessment for a matched thread."""
        source_message_ids = list(
            dict.fromkeys(
                [message.message_id for message in match.messages]
                + match.unresolved_message_ids
            )
        )
        if match.requires_ambiguous_assessment or match.strength == "weak":
            return self._ambiguous(match, source_message_ids)
        if not match.messages:
            return self._assessment(
                ResponseClassification.NO_RESPONSE,
                self._summaries["no_response"],
                match,
                source_message_ids,
                Decimal("1.00"),
            )

        selected = self._latest_relevant(match.messages)
        text = _unquoted_text(f"{selected.subject}\n{selected.body}")
        signals = self._signals(text)
        if len(signals) > 1:
            return self._ambiguous(match, source_message_ids)
        if not signals:
            return self._assessment(
                ResponseClassification.NO_MEANINGFUL_RESPONSE,
                self._summaries["no_meaningful_response"],
                match,
                source_message_ids,
                Decimal("0.85"),
            )

        classification = next(item for item in _PRECEDENCE if item in signals)
        if classification is ResponseClassification.PAYMENT_PROMISED:
            payment_date = _parse_date(text)
            if payment_date is None or _UNSAFE_DATE.search(text):
                return self._ambiguous(match, source_message_ids)
            return self._assessment(
                classification,
                self._summaries[classification.value].format(
                    payment_date=payment_date.isoformat()
                ),
                match,
                source_message_ids,
                Decimal("0.95"),
                payment_date=payment_date,
            )

        details = (
            text if classification is ResponseClassification.DISPUTE_OR_QUERY else None
        )
        requested_action = (
            text if classification is ResponseClassification.DOCUMENT_REQUEST else None
        )
        return self._assessment(
            classification,
            self._summaries[classification.value],
            match,
            source_message_ids,
            Decimal("0.95"),
            dispute_details=details,
            requested_action=requested_action,
        )

    def _latest_relevant(self, messages: list[EmailMessage]) -> EmailMessage:
        ordered = sorted(
            messages, key=lambda message: (message.received_at, message.message_id)
        )
        for message in reversed(ordered):
            text = _unquoted_text(f"{message.subject}\n{message.body}")
            if self._signals(text):
                return message
        return ordered[-1]

    def _signals(self, text: str) -> set[ResponseClassification]:
        normalized = _normalized(text)
        return {
            classification
            for classification in _PRECEDENCE
            if any(
                _normalized(phrase) in normalized
                for phrase in self._phrases[classification.value]
            )
        }

    def _ambiguous(
        self, match: MatchResult, source_message_ids: list[str]
    ) -> ResponseAssessment:
        return self._assessment(
            ResponseClassification.AMBIGUOUS,
            self._summaries["ambiguous"],
            match,
            source_message_ids,
            Decimal("0.50"),
        )

    @staticmethod
    def _assessment(
        classification: ResponseClassification,
        summary: str,
        match: MatchResult,
        source_message_ids: list[str],
        confidence: Decimal,
        *,
        payment_date: date | None = None,
        dispute_details: str | None = None,
        requested_action: str | None = None,
    ) -> ResponseAssessment:
        return ResponseAssessment(
            classification=classification,
            summary=summary,
            payment_date=payment_date,
            dispute_details=dispute_details,
            requested_action=requested_action,
            confidence=confidence,
            source_message_ids=source_message_ids,
            match_reason=match.match_reason,
        )


def _validate_vocabulary(
    vocabulary: object,
) -> tuple[str, Mapping[str, list[str]], Mapping[str, str]]:
    if not isinstance(vocabulary, Mapping):
        raise TypeError("vocabulary must be an object")

    _require_keys(vocabulary, {"version", "phrases", "summary_labels"}, "vocabulary")
    version = vocabulary["version"]
    if not isinstance(version, str) or version not in _SUPPORTED_VOCABULARY_VERSIONS:
        raise ValueError("unsupported vocabulary version")

    phrases = vocabulary["phrases"]
    if not isinstance(phrases, Mapping):
        raise TypeError("phrases must be an object")
    required_phrase_classes = {classification.value for classification in _PRECEDENCE}
    missing_phrase_classes = sorted(required_phrase_classes - set(phrases))
    if missing_phrase_classes:
        raise ValueError(f"missing phrase classes: {', '.join(missing_phrase_classes)}")
    for classification, values in phrases.items():
        if not isinstance(classification, str) or not (
            isinstance(values, list) and all(isinstance(value, str) for value in values)
        ):
            raise TypeError(f"phrases.{classification} must be a list of strings")

    summaries = vocabulary["summary_labels"]
    if not isinstance(summaries, Mapping):
        raise TypeError("summary_labels must be an object")
    missing_summary_labels = sorted(_REQUIRED_SUMMARY_LABELS - set(summaries))
    if missing_summary_labels:
        raise ValueError(f"missing summary labels: {', '.join(missing_summary_labels)}")
    for classification, summary in summaries.items():
        if not isinstance(classification, str) or not isinstance(summary, str):
            raise TypeError(f"summary_labels.{classification} must be a string")

    return version, phrases, summaries


def _require_keys(vocabulary: Mapping[object, object], keys: set[str], section: str) -> None:
    missing_keys = sorted(keys - set(vocabulary))
    if missing_keys:
        raise ValueError(f"missing {section} key: {', '.join(missing_keys)}")
