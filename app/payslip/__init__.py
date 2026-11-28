"""Payslip engine: a salary slip PDF in, a GENUINE / NEEDS_REVIEW / PROBABLY_FAKE / FAKE verdict and its checks out.

Copied from credit_desk-goodskill payslip_check.py on 2026-11-28 (app/payslip/check.py): only the config source
changed (settings from app/config.py instead of dotenv). To update, re-copy it, redo that edit and re-run the
regression check (old vs new on real payslips, Gemini off).
"""
from .check import NotPayslipError, PayslipPasswordError, check_payslip

__all__ = ["check_payslip", "PayslipPasswordError", "NotPayslipError"]
