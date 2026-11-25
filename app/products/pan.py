"""PAN check: format and holder type (4th character). Doesn't confirm the PAN exists or who holds it."""
import re

from pydantic import BaseModel, Field

from . import Product

HOLDER_TYPES = {
    "P": "individual",
    "C": "company",
    "H": "huf",
    "F": "firm",  # includes LLPs
    "A": "aop",
    "T": "trust",
    "B": "boi",
    "L": "local_authority",
    "J": "artificial_juridical_person",
    "G": "government",
}
FORMAT = re.compile(r"[A-Z]{5}[0-9]{4}[A-Z]")


class PanIn(BaseModel):
    pan: str = Field(max_length=20)


def check(raw: str) -> dict:
    pan = raw.strip().upper()
    if len(pan) != 10:
        reason = "length"
    elif not FORMAT.fullmatch(pan):
        reason = "format"
    elif pan[3] not in HOLDER_TYPES:
        reason = "holder_type"
    else:
        reason = None
    return {"pan": pan, "format_valid": reason is None, "reason": reason, "holder_type": HOLDER_TYPES[pan[3]] if reason is None else None}


PRODUCT = Product(
    name="pan", title="PAN check", method="POST", path="/pan/check", price_paise=50,
    input=PanIn, run=lambda db, body, client: check(body.pan),
)
