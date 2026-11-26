"""Date / amount / text helpers tuned for Indian bank statements."""
from __future__ import annotations

import re
from datetime import date, datetime

from .opts import OPTS

MONTHS = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], start=1)}
MONTHS["sept"] = 9

_MON = r"(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sept|Sep|Oct|Nov|Dec)[a-z]*"
# Order matters: longest / most specific first.
DATE_PATTERNS = [
    ("ymd", re.compile(r"(?<!\d)(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})(?!\d)")),
    # a year is never the integer part of an amount: Kotak 'SMS CHAR- 85487865 17-JUL- 48.56' is not 17-Jul-2048
    ("dmy", re.compile(r"(?<!\d)(\d{1,2})[-/.](\d{1,2})[-/.](\d{4}|\d{2})(?![\d/]|\.\d)")),
    ("dMy", re.compile(r"(?<!\d)(\d{1,2})[-/ .]?(" + _MON + r")[-/ .,]{0,2}(\d{4}|\d{2}(?!\s*" + _MON + r"))(?!\d|\.\d)", re.I)),
    ("Mdy", re.compile(r"\b(" + _MON + r")\s+(\d{1,2}),\s*(\d{4})(?!\d)", re.I)),
    # month glued to the day (Bandhan, Apache FOP: 'June22, 2026'): only an exact month name or abbreviation may
    # touch the digits, so a word like 'Marriott12, 2026' never reads as a date
    ("Mdy", re.compile(r"\b(Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|June?|July?|Aug(?:ust)?"
                       r"|Sep(?:t(?:ember)?)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)(\d{1,2}),\s*(\d{4})(?!\d)", re.I)),
]
ANY_DATE_RE = re.compile("|".join(f"(?:{p.pattern})" for _, p in DATE_PATTERNS), re.I)


def _mk(y: int, m: int, d: int) -> date | None:
    if y < 100:
        y += 2000
    if not (1990 <= y <= 2100):
        return None
    try:
        return date(y, m, d)
    except ValueError:
        return None


def parse_date_match(kind: str, g: tuple) -> date | None:
    try:
        if kind == "ymd":
            return _mk(int(g[0]), int(g[1]), int(g[2]))
        if kind == "dmy":
            return _mk(int(g[2]), int(g[1]), int(g[0]))
        if kind == "dMy":
            return _mk(int(g[2]), MONTHS[g[1][:4].lower() if g[1][:4].lower() == "sept" else g[1][:3].lower()], int(g[0]))
        if kind == "Mdy":
            return _mk(int(g[2]), MONTHS[g[0][:3].lower()], int(g[1]))
    except (KeyError, ValueError):
        return None
    return None


def find_dates(text: str) -> list[tuple[int, int, date]]:
    """All dates in text as (start, end, date), non-overlapping, left to right."""
    out: list[tuple[int, int, date]] = []
    taken: list[tuple[int, int]] = []
    for kind, pat in DATE_PATTERNS:
        for m in pat.finditer(text):
            s, e = m.span()
            if any(s < te and e > ts for ts, te in taken):
                continue
            d = parse_date_match(kind, m.groups())
            if d:
                out.append((s, e, d))
                taken.append((s, e))
    for g in OPTS.get().grammars:  # self-heal: a date grammar learned from this document (opts.py)
        for m in g.compiled().finditer(text):
            s, e = m.span()
            if any(s < te and e > ts for ts, te in taken):
                continue
            d = grammar_date(g, m.groups())
            if d:
                out.append((s, e, d))
                taken.append((s, e))
    out.sort()
    return out


def grammar_date(g, groups) -> date | None:
    try:
        v = dict(zip(g.order, groups))
        mo = v["m"]
        mo = MONTHS[mo[:4].lower() if mo[:4].lower() == "sept" else mo[:3].lower()] if g.month_name else int(mo)
        return _mk(int(v["y"]), mo, int(v["d"]))
    except (KeyError, ValueError):
        return None


def parse_date_any(value) -> date | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    s = str(value).strip()
    if not s or s.lower() in ("nan", "nat", "none"):
        return None
    ds = find_dates(s)
    return ds[0][2] if ds else None


# ------------------------------------------------------------------ amounts
AMOUNT_RE = re.compile(
    r"^(?P<sign>[+-])?\(?(?:₹|Rs\.?|INR)?\s?(?P<num>(?:\d{1,3}(?:,\d{2,3})+|\d+)(?:\.\d{1,3})?|\.\d{1,2})\)?"
    r"(?P<mk>\s?\(?(?:Cr|Dr|CR|DR|cr|dr)\.?\)?)?$")
MARKER_RE = re.compile(r"^\(?(Cr|Dr|CR|DR|cr|dr)\.?\)?$")
PLACEHOLDER = {"-", "--", "—", "–", "NIL", "nil", "0", "0.0", "0.00", ".00"}


def parse_amount_token(tok: str):
    """Return (value, marker, has_decimal, sign) or None. value is abs float."""
    t = tok.strip().replace("₹", "").replace("₹", "")
    if not t:
        return None
    m = AMOUNT_RE.match(t)
    if not m:
        return None
    num = m.group("num")
    digits = num.replace(",", "").replace(".", "")
    has_dec = "." in num
    has_comma = "," in num
    if not has_dec and not has_comma:
        # bare integers: allow small/medium amounts only; long ones are refs / account numbers
        if len(digits) > 8 or (len(digits) > 1 and digits.startswith("0")):
            return None
    try:
        val = float(num.replace(",", ""))
    except ValueError:
        return None
    mk = m.group("mk")
    marker = None
    if mk:
        marker = "CR" if "c" in mk.lower() else "DR"
    sign = m.group("sign")
    if t.startswith("(") and t.rstrip(".").endswith(")") and not mk:
        sign = "-"
    return val, marker, has_dec or has_comma, sign


def parse_amount_cell(value) -> float | None:
    """Excel/CSV cell -> float (abs, sign kept if negative)."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        if value != value:  # NaN
            return None
        return float(value)
    s = str(value).strip().replace(",", "").replace("₹", "").replace("INR", "").replace("Rs.", "").strip()
    if not s or s in PLACEHOLDER and s not in ("0", "0.0", "0.00") or s.lower() in ("nan", "none"):
        return None
    neg = s.startswith("-") or (s.startswith("(") and s.endswith(")"))
    mk = None
    low = s.lower()
    if low.endswith("cr") or low.endswith("cr."):
        mk = "CR"
    elif low.endswith("dr") or low.endswith("dr."):
        mk = "DR"
    s2 = re.sub(r"[^\d.]", "", s)
    if not s2 or s2 == ".":
        return None
    try:
        v = float(s2)
    except ValueError:
        return None
    if neg or mk == "DR":
        v = -v
    return v


def norm(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").upper()).strip()


def has_any(text_upper: str, words) -> str | None:
    for w in words:
        if w in text_upper:
            return w
    return None


def month_key(d: date) -> str:
    return f"{d.year:04d}-{d.month:02d}"


def mask_account(acc: str | None) -> str | None:
    if not acc:
        return acc
    a = re.sub(r"\s", "", acc)
    if len(a) <= 4:
        return a
    return "X" * (len(a) - 4) + a[-4:]
