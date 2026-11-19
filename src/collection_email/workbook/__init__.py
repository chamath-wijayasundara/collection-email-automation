"""Workbook ingestion and validation."""

from collection_email.workbook.reader import fingerprint_file, read_workbook

__all__ = ["fingerprint_file", "read_workbook"]
