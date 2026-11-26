"""BSA (Bank Statement Analyser): a bank's PDF statement in, the full analysis out (app/bsa/, REPORT_SCHEMA.md).

POST /v1/bsa/analyze is multipart (field `file`, optional `password`), so api.py and account.py mount it by hand.
Only a successful analysis (200) is charged. Nothing is stored except the usual ApiCall billing row: the PDF lives in
a temp file for the length of the call and the report goes straight back to the caller.
"""
import os
import tempfile
import threading
from contextlib import contextmanager

from pydantic import BaseModel, Field, field_validator
from sqlalchemy.orm import Session

from ..bsa import AAStatement, PasswordRequired, UnsupportedFile
from ..bsa.isolated import AnalysisTimeout, analyze_isolated
from ..config import get_settings
from . import Product, Rejected

MAX_PDF_BYTES = 10 * 1024 * 1024

# Load limit: at most MAX_PARALLEL analyses at once (memory), QUEUE_MAX more waiting up to QUEUE_WAIT_S for a slot.
# The routes take a slot (api.run_bsa) before anything is charged, so Busy is always free.
MAX_PARALLEL = get_settings().bsa_max_parallel
QUEUE_MAX = get_settings().bsa_queue_max
QUEUE_WAIT_S = get_settings().bsa_queue_wait
_slots = threading.BoundedSemaphore(MAX_PARALLEL)
_lock = threading.Lock()
_waiting = 0


class Busy(Exception):
    """No analysis slot freed up in time, or the queue is full."""


@contextmanager
def slot():
    global _waiting
    if not _slots.acquire(blocking=False):
        with _lock:
            if _waiting >= QUEUE_MAX:
                raise Busy
            _waiting += 1
        try:
            got = _slots.acquire(timeout=QUEUE_WAIT_S)
        finally:
            with _lock:
                _waiting -= 1
        if not got:
            raise Busy
    try:
        yield
    finally:
        _slots.release()


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
