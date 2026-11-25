"""GSTIN check: format, state code and check digit of a regular (PAN-based) GSTIN.
Doesn't confirm the GSTIN is registered or active on the GST portal.
"""
import re

from pydantic import BaseModel, Field

from . import Product

STATES = {
    "01": "Jammu and Kashmir", "02": "Himachal Pradesh", "03": "Punjab", "04": "Chandigarh", "05": "Uttarakhand",
    "06": "Haryana", "07": "Delhi", "08": "Rajasthan", "09": "Uttar Pradesh", "10": "Bihar", "11": "Sikkim",
    "12": "Arunachal Pradesh", "13": "Nagaland", "14": "Manipur", "15": "Mizoram", "16": "Tripura", "17": "Meghalaya",
    "18": "Assam", "19": "West Bengal", "20": "Jharkhand", "21": "Odisha", "22": "Chhattisgarh", "23": "Madhya Pradesh",
    "24": "Gujarat", "25": "Daman and Diu", "26": "Dadra and Nagar Haveli and Daman and Diu", "27": "Maharashtra",
    "28": "Andhra Pradesh (old)", "29": "Karnataka", "30": "Goa", "31": "Lakshadweep", "32": "Kerala",
    "33": "Tamil Nadu", "34": "Puducherry", "35": "Andaman and Nicobar Islands", "36": "Telangana",
    "37": "Andhra Pradesh", "38": "Ladakh", "97": "Other Territory", "99": "Centre Jurisdiction",
}
# 2-digit state, 10-char PAN, entity number (1-9, A-Z), "Z", check character.
FORMAT = re.compile(r"[0-9]{2}[A-Z]{5}[0-9]{4}[A-Z][1-9A-Z]Z[0-9A-Z]")
CHARS = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ"


def check_char(first14: str) -> str:
    """GSTN's base-36 check character: odd positions weigh 1, even positions 2, digits of each product summed."""
    total = 0
    for i, ch in enumerate(first14):
        v = CHARS.index(ch) * (2 if i % 2 else 1)
        total += v // 36 + v % 36
    return CHARS[(36 - total % 36) % 36]


class GstinIn(BaseModel):
    gstin: str = Field(max_length=30)


def check(raw: str) -> dict:
    gstin = raw.strip().upper()
    if len(gstin) != 15:
        reason = "length"
    elif not FORMAT.fullmatch(gstin):
        reason = "format"
    elif gstin[:2] not in STATES:
        reason = "state_code"
    elif check_char(gstin[:14]) != gstin[14]:
        reason = "check_digit"
    else:
        reason = None
    ok = reason is None
    return {"gstin": gstin, "format_valid": ok, "reason": reason, "state": STATES[gstin[:2]] if ok else None, "pan": gstin[2:12] if ok else None}


PRODUCT = Product(
    name="gstin", title="GSTIN check", method="POST", path="/gstin/check", price_paise=100,
    input=GstinIn, run=lambda db, body, client: check(body.gstin),
)
