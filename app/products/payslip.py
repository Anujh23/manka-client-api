"""Payslip Checker: a salary slip PDF in, a verdict and its checks out (app/payslip/, copied from the owner's checker).

POST /v1/payslip/check is multipart (field `file`, optional `password`), so api.py and account.py mount it by hand.
Only a successful check (200) is charged. Nothing is stored except the usual ApiCall billing row. Clients get the
same report the owner's credit desk shows (verdict, risk, review summary, red flags, checks, IDs, figures, PDF details),
never which tools helped: AI / vendor wording is dropped or neutralised here.
"""
import re

from pydantic import BaseModel, Field, field_validator
from pypdf.errors import PyPdfError
from sqlalchemy.orm import Session

from ..config import get_settings
from ..limits import Limit
from ..payslip import NotPayslipError, PayslipPasswordError, check_payslip
from . import Product, Rejected

MAX_PDF_BYTES = 10 * 1024 * 1024

_s = get_settings()
LIMIT = Limit(_s.payslip_max_parallel, _s.payslip_queue_max, _s.payslip_queue_wait)
slot = LIMIT.slot


class PayslipIn(BaseModel):
    pdf: bytes = Field(max_length=MAX_PDF_BYTES)
    password: str | None = Field(default=None, max_length=200)
    filename: str | None = Field(default=None, max_length=300)

    @field_validator("pdf")
    @classmethod
    def is_pdf(cls, v: bytes) -> bytes:
        if not v.startswith(b"%PDF"):
            raise ValueError("Upload a PDF.")
        return v


_VENDOR = re.compile(r"gemini|google|\bAI\b|model", re.I)
RISK = {"GENUINE": "LOW", "NEEDS_REVIEW": "MEDIUM", "PROBABLY_FAKE": "HIGH", "FAKE": "VERY_HIGH"}
METADATA = ("producer", "creator", "xmp_tools", "author", "title", "created", "modified", "pages", "pdf_version",
            "encrypted", "saves", "fonts", "size_kb")


def _clean(text: str) -> str:
    """Public wording for a check line: no AI / vendor names."""
    text = re.sub(r"\s*\(confirmed by AI[^)]*\)", "", text)
    text = re.sub(r"google (docs|sheets)", "an online editor", text, flags=re.I)
    return text


def _clean_value(v):
    """PDF details keep the file's own values; only vendor product names are neutralised, as in the checks."""
    if isinstance(v, str):
        return _clean(v)
    if isinstance(v, list):
        return [_clean(x) if isinstance(x, str) else x for x in v]
    return v


def _lines(items) -> list[str]:
    return [t for t in map(_clean, filter(None, items or [])) if not _VENDOR.search(t)]


def public(result: dict) -> dict:
    """The credit desk's report, minus how it was made: no AI model, mode, error or text sent anywhere."""
    checks = []
    for c in result["checks"]:
        label, detail = _clean(c["label"]), _clean(c["detail"])
        if _VENDOR.search(label) or _VENDOR.search(detail):
            continue
        checks.append({"label": label, "status": c["status"], "detail": detail})
    review = result.get("ai") or {}
    summary = _lines([review.get("summary")])
    m = result.get("metadata") or {}
    return {
        "verdict": result["verdict"],
        "risk": RISK[result["verdict"]],
        "confidence": review.get("confidence"),
        "summary": summary[0] if summary else None,
        "red_flags": _lines(review.get("red_flags")),
        "looks_fine": _lines(review.get("positives")),
        "unlocked": bool(result.get("unlocked")),  # password removed / re-saved: the file's origin can't be checked
        "checks": checks,
        "ids": result["ids"],
        "payslips": result["payslips"],
        "metadata": {k: _clean_value(m.get(k)) for k in METADATA},
    }


def check(db: Session, body: PayslipIn, client) -> dict:
    try:
        result = check_payslip(body.pdf, body.password or "", body.filename or "")
    except PayslipPasswordError:
        msg = "Wrong password for this payslip." if body.password else "This payslip is password protected. Send its password."
        raise Rejected(422, "password_required", msg) from None
    except NotPayslipError as e:
        kind = e.doc_type if e.doc_type and not e.doc_type.lower().startswith("other") else None
        raise Rejected(422, "not_payslip", "This is not a payslip." + (f" It looks like {_a(kind)}." if kind else "")) from None
    except PyPdfError:  # damaged / unreadable PDF
        raise Rejected(422, "unsupported_file", "Could not read this PDF. Send the payslip as issued.") from None
    return public(result)


def _a(noun: str) -> str:
    return ("an " if noun[:1].lower() in "aeiou" else "a ") + noun


PRODUCT = Product(
    name="payslip", title="Payslip Checker", method="POST", path="/payslip/check", price_paise=1000,
    input=PayslipIn, run=check,
)
