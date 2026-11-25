"""IFSC lookup: bank, branch, address and payment modes, from the open Razorpay IFSC dataset (MIT licence).
Data is loaded with `python -m app.cli load-ifsc`; data_as_of in every response is that release's date.
"""
import csv
import re
from collections.abc import Iterable
from datetime import date

from pydantic import BaseModel, field_validator
from sqlalchemy import delete, insert
from sqlalchemy.orm import Session

from ..models import Dataset, IfscBranch
from . import NotFound, Product

FORMAT = re.compile(r"[A-Z]{4}0[A-Z0-9]{6}")


class IfscIn(BaseModel):
    ifsc: str

    @field_validator("ifsc")
    @classmethod
    def _format(cls, v: str) -> str:
        v = v.strip().upper()
        if not FORMAT.fullmatch(v):
            raise ValueError("Enter an 11-character IFSC: 4 letters, 0, then 6 letters or digits.")
        return v


def lookup(db: Session, body: IfscIn, client=None) -> dict:
    b = db.get(IfscBranch, body.ifsc)
    if b is None:
        raise NotFound("IFSC not found.")
    ds = db.get(Dataset, "ifsc")
    return {
        "ifsc": b.ifsc, "bank": b.bank, "branch": b.branch, "address": b.address,
        "city": b.city, "district": b.district, "state": b.state, "micr": b.micr,
        "upi": b.upi, "neft": b.neft, "rtgs": b.rtgs, "imps": b.imps,
        "data_as_of": ds.as_of.isoformat() if ds else None,
    }


def load(db: Session, lines: Iterable[str], version: str, as_of: date, batch: int = 2000) -> int:
    """Replace all branches with the rows of a Razorpay IFSC.csv, in one transaction (lookups keep working).
    Each batch is one multi-row INSERT (one round trip), so loading a remote Postgres takes about a minute."""
    db.execute(delete(IfscBranch))
    rows, count = [], 0
    for r in csv.DictReader(lines):
        rows.append({
            "ifsc": r["IFSC"].strip().upper(), "bank": r["BANK"].strip() or None,
            "branch": r["BRANCH"].strip()[:120], "address": r["ADDRESS"].strip()[:300],
            "city": r["CITY"].strip()[:60], "district": r["DISTRICT"].strip()[:60], "state": r["STATE"].strip()[:60],
            "micr": r["MICR"].strip() or None,
            **{k.lower(): r[k].strip().lower() == "true" for k in ("UPI", "NEFT", "RTGS", "IMPS")},
        })
        if len(rows) == batch:
            db.execute(insert(IfscBranch).values(rows))
            count, rows = count + len(rows), []
    if rows:
        db.execute(insert(IfscBranch).values(rows))
        count += len(rows)
    db.merge(Dataset(name="ifsc", version=version, as_of=as_of))
    db.commit()
    return count


PRODUCT = Product(
    name="ifsc", title="IFSC lookup", method="GET", path="/ifsc/{ifsc}", price_paise=50,
    input=IfscIn, run=lookup,
)
