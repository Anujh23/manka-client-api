"""BSA engine: parse -> reconcile -> forensics -> analytics. Entry point: analyze_file().

Copied from the BSA project on 2026-11-26, PDF-only (no spreadsheet reader, no pandas). Engine logic is validated
against an answer key: don't edit it here. To update, re-copy bsa/engine from the BSA project (keep the PDF-only
edit in pipeline.py and the app.bsa paths in isolated.py) and re-run the regression check against data/samples.
"""
from .pipeline import ENGINE_VERSION, AAStatement, PasswordRequired, UnsupportedFile, analyze_file, is_aa_statement

__all__ = ["analyze_file", "PasswordRequired", "UnsupportedFile", "AAStatement", "is_aa_statement", "ENGINE_VERSION"]
