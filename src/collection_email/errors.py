"""Application-specific exceptions."""


class FileValidationError(Exception):
    """A file-level validation failure that maps to CLI exit code 2."""


class MissingResponseAssessmentError(Exception):
    """An eligible invoice reached decisioning without response evidence."""
