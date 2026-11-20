"""Deterministic customer-response classification."""

from collection_email.responses.classifier import (
    ResponseClassifier,
    RuleBasedResponseClassifier,
)

__all__ = ["ResponseClassifier", "RuleBasedResponseClassifier"]
