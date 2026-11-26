"""BSA (Bank Statement Analyser): a bank's PDF statement in, the full analysis out (app/bsa/, REPORT_SCHEMA.md).

POST /v1/bsa/analyze is multipart (field `file`, optional `password`), so api.py and account.py mount it by hand.
Only a successful analysis (200) is charged. Nothing is stored except the usual ApiCall billing row: the PDF lives in
a temp file for the length of the call and the report goes straight back to the caller.
"""
import os
import tempfile

from pydantic import BaseModel, Field, field_validator
from sqlalchemy.orm import Session

from ..bsa import AAStatement, PasswordRequired, UnsupportedFile
from ..bsa.isolated import AnalysisTimeout, analyze_isolated
from . import Product, Rejected

MAX_PDF_BYTES = 10 * 1024 * 1024


class StatementIn(BaseModel):
    pdf: bytes = Field(max_length=MAX_PDF_BYTES)
    password: str | None = Field(default=None, max_length=200)
    filename: str | None = Field(default=None, max_length=300)

    @field_validator("pdf")
    @classmethod
    def is_pdf(cls, v: bytes) -> bytes:
        if not v.startswith(b"%PDF"):
            raise ValueError("Upload a PDF.")
        return v


def analyze(db: Session, body: StatementIn, client) -> dict:
    fd, path = tempfile.mkstemp(suffix=".pdf")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(body.pdf)
        return analyze_isolated(path, password=body.password or None, original_filename=body.filename)
    except PasswordRequired:
        msg = "Wrong password for this PDF." if body.password else "This PDF is password protected. Send its password."
        raise Rejected(422, "password_required", msg) from None
    except AAStatement as e:
        raise Rejected(422, "aa_statement", str(e)) from None
    except UnsupportedFile as e:
        raise Rejected(422, "unsupported_file", f"Could not read this statement: {e}") from None
    except AnalysisTimeout as e:
        raise Rejected(422, "analysis_timeout", str(e)) from None
    finally:
        try:
            os.remove(path)
        except OSError:
            pass


PRODUCT = Product(
    name="bsa", title="BSA", method="POST", path="/bsa/analyze", price_paise=2500,
    input=StatementIn, run=analyze,
)
