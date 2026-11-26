"""Generic, bank-agnostic PDF statement parser.

Idea: almost every Indian bank statement row is
    [serial] DATE [value date] ... narration ... [ref] [value date] AMOUNT(S) BALANCE [junk]
so we (1) rebuild visual lines from word boxes, (2) find "anchor" lines that start with a
date, (3) read the numeric tail of each row, and (4) let the balance chain (reconcile.py)
decide debit vs credit and verify every row. Header geometry (Debit/Credit/Balance column
positions) is used only as a hint.
"""
from __future__ import annotations

import re
import statistics
from datetime import date, timedelta

import fitz  # PyMuPDF

from .models import Cand, ParseResult, RawTxn
from .opts import OPTS, check_deadline
from .utils import MARKER_RE, MONTHS, find_dates, parse_amount_token

CID_RE = re.compile(r"\(cid:\d+\)")
SERIAL_RE = re.compile(r"^\s*(\d{1,6}[.)]?\s+)?")
# AU Small Finance prints the bank reference before the date: a UTR (4-letter bank code + R/H/N + digits,
# "INDBR2202609080", "AUBLH113241312") and an Excel text-prefix apostrophe ("'611418703986")
ID_TOKEN_RE = re.compile(r"^('?[A-Z]{0,3}\d{3,14}|'?[A-Z]{4}[RHN]\d{6,14}|\d{1,6}[.)]?)$")
_MONS = r"(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Sept|Oct|Nov|Dec)[a-z]*"
WRAPPED_DM_RE = re.compile(r"^(\d{1,2}[.)]?\s+)?(\d{1,2})[ -](" + _MONS + r")(?:\s+(\d{1,2})[ -](" + _MONS + r"))?(?:\s|$)", re.I)
YEAR_START_RE = re.compile(r"^(\d{4})(?:\s+(\d{4}))?(?:\s|$)")


def _fix_wrapped_dates(lines: list["Line"]):
    """SBI-style cells where '15 Aug' sits on one line and '2025' on the next: move the year up."""
    for i in range(len(lines) - 1):
        ln, nx = lines[i], lines[i + 1]
        m = WRAPPED_DM_RE.match(ln.text)
        if not m:
            continue
        y = YEAR_START_RE.match(nx.text)
        if not y or (nx.top - ln.bottom) > 2.5 * max(1.0, ln.bottom - ln.top):
            continue
        years = [g for g in y.groups() if g]
        # find month words in this line and append the year after each
        new_words, yi = [], 0
        for w in ln.words:
            new_words.append(w)
            if yi < len(years) and re.fullmatch(_MONS, w.t, re.I) and (len(new_words) <= 5):
                new_words.append(W(years[yi], w.x1 + 1, w.x1 + 20, w.top, w.bottom))
                yi += 1
        if yi == 0:
            continue
        ln.words = new_words
        ln.text = " ".join(w.t for w in ln.words)
        nx.words = nx.words[yi:]
        nx.text = " ".join(w.t for w in nx.words)
    return [l for l in lines if l.words]


MON_WORD_RE = re.compile(r"^" + _MONS + r"$", re.I)
DAY_WORD_RE = re.compile(r"^\d{1,2}$")
YEAR_WORD_RE = re.compile(r"^(\d{4}|\d{2})$")
PERIOD_HINT_RE = re.compile(r"\b(from|to|period|statement)\b", re.I)


def _statement_period(lines: list["Line"]):
    """'STATEMENT DATE : 01 Sep 2026 To 29 Oct 2026' -> (from, to) from the page-1 header."""
    for ln in lines[:60]:
        if not PERIOD_HINT_RE.search(ln.text):
            continue
        ds = find_dates(ln.text)
        if len(ds) >= 2 and ds[0][2] < ds[1][2] and (ds[1][2] - ds[0][2]).days <= 800:
            return ds[0][2], ds[1][2]
    return None


def _yearless_pairs(ln: "Line") -> list[int]:
    """Word indices of up to two leading 'Sep 01' / '01 Sep' dates printed without a year (Standard Chartered)."""
    ws, out, i = ln.words, [], 0
    if ws and ID_TOKEN_RE.match(ws[0].t) and len(ws) > 2 and MON_WORD_RE.match(ws[1].t):
        i = 1  # serial number before the date
    while len(out) < 2 and i + 1 < len(ws):
        a, b = ws[i].t, ws[i + 1].t
        if not ((MON_WORD_RE.match(a) and DAY_WORD_RE.match(b)) or (DAY_WORD_RE.match(a) and MON_WORD_RE.match(b))):
            break
        if i + 2 < len(ws) and YEAR_WORD_RE.match(ws[i + 2].t):
            break  # has a year after all -> normal date parsing handles it
        out.append(i)
        i += 2
    return out


def _fix_yearless_dates(pages_lines: list[list["Line"]]):
    """Rows dated 'Sep 01' with the year only in the statement period. Rewrite each leading pair into one
    'DD-Mon-YYYY' word, year taken from the period. Only when such rows clearly dominate the document, the
    period is printed, the date sits in a recurring date column and the date falls inside the period."""
    if not pages_lines:
        return
    period = _statement_period(pages_lines[0])
    if not period:
        return
    p_from, p_to = period
    found, full_lead = [], 0
    for lines in pages_lines:
        for ln in lines:
            idx = _yearless_pairs(ln)
            if idx:
                found.append((ln, idx))
            else:
                ds = find_dates(ln.text)
                if ds and ds[0][0] <= 8:
                    full_lead += 1
    if len(found) < 3 or len(found) <= 2 * full_lead:
        return
    # date columns: x0 positions shared by at least 3 leading dates (narration lines that happen to start
    # with 'May 15' sit elsewhere)
    xs = [round(ln.words[k].x0) for ln, idx in found for k in idx]
    cols = [x for x in set(xs) if sum(abs(x - y) <= 5 for y in xs) >= 3]
    prev = p_from
    for ln, idx in found:
        if any(not any(abs(ln.words[k].x0 - c) <= 5 for c in cols) for k in idx):
            continue
        new_words, conv, last = [], [], None
        k = 0
        while k < len(ln.words):
            if k in idx:
                a, b = ln.words[k], ln.words[k + 1]
                mon, day = (a.t, b.t) if MON_WORD_RE.match(a.t) else (b.t, a.t)
                m = MONTHS[mon[:4].lower() if mon[:4].lower() == "sept" else mon[:3].lower()]
                cands = []
                for y in range(p_from.year, p_to.year + 1):
                    try:
                        dt = date(y, m, int(day))
                    except ValueError:
                        continue
                    if p_from - timedelta(days=3) <= dt <= p_to + timedelta(days=3):
                        cands.append(dt)
                if not cands:
                    conv = None
                    break
                dt = min(cands, key=lambda c: abs((c - prev).days))
                conv.append(dt)
                last = dt
                new_words.append(W(f"{dt.day:02d}-{MON_ABBR[dt.month - 1]}-{dt.year}", a.x0, b.x1,
                                   min(a.top, b.top), max(a.bottom, b.bottom)))
                k += 2
            else:
                new_words.append(ln.words[k])
                k += 1
        if not conv:
            continue
        prev = last
        ln.words = new_words
        ln.text = " ".join(w.t for w in new_words)


MON_ABBR = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
TAIL_JUNK_RE = re.compile(r"^(\(\d{1,6}\)|\*+|\|)$")
PLACEHOLDER_TOKENS = {"-", "--", "—", "–", "NIL", "nil", "|"}
CURRENCY_TOKENS = {"INR", "RS", "RS.", "₹"}
OPENING_RE = re.compile(r"(opening\s*bal(ance)?|balance\s*b/?f|brought\s*forward|balance\s*as\s*on|\bb/f\b|op\.?\s*bal)", re.I)
# 'Totals / Balance :- <dr> <cr> <bal>' (Saraswat) closes the table: it is not the last row's narration
SKIP_RE = re.compile(r"(\btotals?\b|closing\s*bal|carried\s*forward|\bc/f\b|statement\s*summary|page\s*total|"
                     r"\bpage\s*\d+\s*(of|/)\s*\d+|generated\s*on|computer\s*generated|end\s*of\s*statement|"
                     r"\(dd[/-]mm[/-]yy(?:yy)?\))", re.I)  # a wrapped / repeated header: 'Date (dd/mm/yyyy)'
PAGE_FOOTER_RE = re.compile(r"\b\d{1,2}:\d{2}\b.*\bpage\s*\d+\s*(?:of|/)\s*\d+\s*$", re.I)  # print time first  # '31/08/2026 2:30 pm 6 Page 6 of 7'
DIR_WORDS = {"CREDIT": "CR", "CR": "CR", "CR.": "CR", "DEPOSIT": "CR", "C": None,
             "DEBIT": "DR", "DR": "DR", "DR.": "DR", "WITHDRAWAL": "DR", "D": None}
HEADER_DESC_WORDS = ("date", "txn", "transaction", "particulars", "narration", "description", "details", "remarks")


class W:
    __slots__ = ("t", "x0", "x1", "top", "bottom")

    def __init__(self, t, x0, x1, top, bottom):
        self.t, self.x0, self.x1, self.top, self.bottom = t, x0, x1, top, bottom

    @property
    def mid(self):
        return (self.top + self.bottom) / 2


class Line:
    __slots__ = ("page", "words", "text", "top", "bottom", "date_word_idx", "kind", "cands", "rest", "anchor_dates",
                 "dir_hint")

    def __init__(self, page, words):
        self.page = page
        self.words = sorted(words, key=lambda w: w.x0)
        self.text = " ".join(w.t for w in self.words)
        self.top = min(w.top for w in words)
        self.bottom = max(w.bottom for w in words)
        self.date_word_idx: set[int] = set()
        self.kind = "narr"
        self.cands: list[Cand] = []
        self.rest: list[W] = []
        self.anchor_dates = []
        self.dir_hint = None

    @property
    def mid(self):
        return (self.top + self.bottom) / 2


def reading_frame(page, words=None):
    """Statements printed sideways (SBM's JasperReports pages: /Rotate 90 on a landscape mediabox, so every line
    runs bottom-to-top) give word boxes where 'left to right' is really 'bottom to top'. Return a function mapping a
    (x0, y0, x1, y1) box into the reading frame (text runs left to right, lines stack downwards), or None when the
    page already reads normally. Only pages whose long words are mostly taller than wide pay for the dict pass."""
    if words is None:
        words = page.get_text("words")
    long_w = [w for w in words if len(w[4]) >= 4]
    if len(long_w) < 20 or sum((w[3] - w[1]) > 1.5 * (w[2] - w[0]) for w in long_w) < 0.8 * len(long_w):
        return None
    return frame_from_dict(page, page.get_text("dict"))


def frame_from_dict(page, d: dict):
    """reading_frame() from an already extracted get_text('dict') (forensics maps span boxes the same way)."""
    dirs: dict[tuple[int, int], int] = {}
    for b in d.get("blocks", []):
        for ln in b.get("lines", []):
            k = (round(ln["dir"][0]), round(ln["dir"][1]))
            dirs[k] = dirs.get(k, 0) + sum(len(s.get("text", "")) for s in ln.get("spans", []))
    if not dirs:
        return None
    main = max(dirs, key=dirs.get)
    if dirs[main] < 0.8 * sum(dirs.values()):
        return None
    W_, H_ = page.rect.width, page.rect.height
    if main == (0, -1):  # text runs upwards, its top points left
        return lambda b: (H_ - b[3], b[0], H_ - b[1], b[2])
    if main == (0, 1):  # text runs downwards, its top points right
        return lambda b: (b[1], W_ - b[2], b[3], W_ - b[0])
    return None


def _page_words(page) -> list[W]:
    raw = page.get_text("words")
    fx = reading_frame(page, raw)
    out = []
    for x0, y0, x1, y1, t, *_ in raw:
        t = CID_RE.sub("", t).strip()
        if t:
            if fx:
                x0, y0, x1, y1 = fx((x0, y0, x1, y1))
            out.append(W(t, x0, x1, y0, y1))
    return out


def _group_lines(page_no: int, words: list[W]) -> list[Line]:
    if not words:
        return []
    hs = [w.bottom - w.top for w in words if w.bottom > w.top]
    tol = max(1.5, 0.45 * (statistics.median(hs) if hs else 8))
    words = sorted(words, key=lambda w: (w.mid, w.x0))
    groups: list[list[W]] = []
    cur_mid = None
    for w in words:
        if groups and abs(w.mid - cur_mid) <= tol:
            groups[-1].append(w)
        else:
            groups.append([w])
            cur_mid = w.mid
    return [Line(page_no, g) for g in groups]


# ------------------------------------------------------------------ header / layout
def _role(word: str) -> str | None:
    w = word.lower().strip(":")
    if "/" in w and any(k in w for k in ("debit", "credit", "dr", "cr")) and not w.startswith("balance"):
        return "type"
    if w.startswith("balance") or w in ("bal", "bal."):
        return "balance"
    if w.startswith(("debit", "withdrawal", "withdrawl", "withdrawn")) or w in ("dr", "dr.", "(dr)", "(dr.)", "withdraw"):  # UCO 'WITHDRAW'/'ALS'
        return "debit"
    if w.startswith(("credit", "deposit")) or w in ("cr", "cr.", "(cr)", "(cr.)"):
        return "credit"
    if w.startswith(("amount", "amt")):
        return "amount"
    if w == "type":
        return "type"
    return None


def _detect_header(line: Line) -> dict | None:
    low = line.text.lower()
    if "opening" in low or "closing balance" in low and "date" not in low:
        return None
    if not any(k in low for k in HEADER_DESC_WORDS):
        return None
    ws = line.words
    if len(ws) >= 20:
        # a sentence of fine print, not column titles (longest real header seen: 17 words). IDBI's last page
        # 'date for Debit and Credits in the account. Balance shown includes debit, credit, ..' became the file's
        # layout: its money columns at x 74..224 pushed every wrapped description line out of the narration column
        return None
    roles: dict[str, tuple[float, float]] = {}
    k = 0
    while k < len(ws):
        w = ws[k]
        r = _role(w.t)
        x1 = w.x1
        if r in ("debit", "credit", "balance"):
            # multi-word label 'Debit Amount (Dr)': the column spans the whole label (amounts are right-aligned
            # under its end), so swallow following 'Amount' / same-role words that sit right next to it
            while k + 1 < len(ws) and ws[k + 1].x0 - x1 <= 12 and _role(ws[k + 1].t) in ("amount", r):
                k += 1
                x1 = ws[k].x1
        if r and r not in roles:
            roles[r] = (w.x0, x1)
        k += 1
    if "balance" not in roles:
        return None
    if ("debit" in roles and "credit" in roles) or "amount" in roles:
        if "debit" in roles and "credit" in roles:
            roles.pop("amount", None)
        # a text column printed right of Balance ('Channel' UPI/IMPS on Bank of Maharashtra, 'Remarks', 'Branch'):
        # its words must not end the right-to-left amount scan
        after = [w.x0 for w in ws if w.x0 > roles["balance"][1] + 5 and _role(w.t) is None
                 and re.fullmatch(r"[A-Za-z][A-Za-z./]{3,}", w.t)]
        if after:
            roles["_after_balance"] = (min(after), min(after))
            # PNB (JasperReports OpTransactionHistory): 'Date Cheque Withdrawal Deposit Balance Narration' - the
            # description column itself is printed right of Balance, and nothing left of the money columns
            desc = ("narration", "particulars", "description", "details")
            if (any(w.t.lower().strip(":") in desc and w.x0 > roles["balance"][1] + 5 for w in ws)
                    and not any(w.t.lower().strip(":") in desc and w.x0 < roles["balance"][0] for w in ws)):
                roles["_narr_after"] = True
        # 'Post Date | Value Date | Branch Code | Cheque Number | Transaction Description' (Central Bank): a bare
        # branch code ('4519') sits in its own column left of the description; its span ends at the next title
        desc_x = [w.x0 for w in ws if w.t.lower().strip(":") in ("description", "particulars", "narration")]
        for j, w in enumerate(ws):
            if (w.t.lower() == "branch" and j + 1 < len(ws) and desc_x and ws[j + 1].x0 < min(desc_x)
                    and _role(ws[j + 1].t) is None):
                roles["_branch_col"] = (w.x0 - 15, ws[j + 1].x0)
                break
        drcr = [w for w in ws if w.x0 > roles["balance"][1] and "/" in w.t and _role(w.t) == "type"]
        if drcr:  # 'Balance  Dr/Cr' (Federal): see _type_markers_after_balance
            roles["_drcr_after_balance"] = (drcr[0].x0, drcr[0].x1)
        return roles
    return None


def _detect_header_2line(line: Line, nxt: Line) -> dict | None:
    """Header whose Balance title wraps onto a second line ('Debit Credit Total' / 'Number Balance'): the first line
    has the description + debit/credit (or amount) titles, the line right under it supplies 'Balance'."""
    if (nxt.page != line.page or nxt.top - line.bottom > 1.5 * max(1.0, line.bottom - line.top)
            or not any(_role(w.t) == "balance" for w in nxt.words)
            or any(_role(w.t) == "balance" for w in line.words)):
        return None
    if not ({"debit", "credit"} <= {_role(w.t) for w in line.words} or any(_role(w.t) == "amount" for w in line.words)):
        return None
    return _detect_header(Line(line.page, line.words + nxt.words))


def _assign_col(c: Cand, layout: dict | None) -> str | None:
    if not layout:
        return None
    cx = (c.x0 + c.x1) / 2
    best, bd = None, 1e9
    for role in ("debit", "credit", "amount", "balance"):
        if role not in layout:
            continue
        x0, x1 = layout[role]
        d = min(abs(cx - (x0 + x1) / 2), abs(c.x1 - x1))
        if d < bd:
            best, bd = role, d
    return best if bd < 90 else None


def _type_markers_after_balance(raws: list[RawTxn], layout: dict | None, opening: float | None) -> int:
    """Federal Bank prints a 'Dr/Cr' column right of Balance that is the row's direction (withdrawal -> Dr,
    deposit -> Cr), not the sign of the balance ('500.00  180.26 Dr' after an opening of 680.26). Read as a sign,
    every withdrawal row would show an overdrawn balance. Move the marker from the balance to the amount only when
    (a) the 'Dr/Cr' title sits right of 'Balance', (b) the marker follows the amount's column on >= 95% of rows,
    with both Dr and Cr present, and (c) the balance chain fits better with unsigned balances.
    Returns the number of rows changed."""
    if not layout or "_drcr_after_balance" not in layout:
        return 0
    rows = [r for r in raws if len(r.cands) >= 2 and r.cands[-1].marker and not r.cands[-1].sign
            and r.cands[-2].col in ("debit", "credit")]
    if len(rows) < 10:
        return 0
    agree = sum(1 for r in rows if r.cands[-1].marker == ("DR" if r.cands[-2].col == "debit" else "CR"))
    if agree < 0.95 * len(rows) or len({r.cands[-1].marker for r in rows}) < 2:
        return 0

    def fits(signed: bool) -> int:
        prev, n = opening, 0
        for r in raws:
            if len(r.cands) < 2:
                continue
            b = r.cands[-1]
            bal = -b.value if (signed and b.marker == "DR") or b.sign == "-" else b.value
            if prev is not None and abs(abs(bal - prev) - r.cands[-2].value) <= 0.01:
                # unsigned reading must also move the balance the way the marker says (Cr up, Dr down): on an
                # overdrawn account printed as magnitudes the unsigned chain fits too, with every direction inverted
                if signed or not b.marker or (bal > prev) == (b.marker == "CR"):
                    n += 1
            prev = bal
        return n

    if fits(False) <= fits(True):
        return 0
    for r in rows:
        b = r.cands[-1]
        if not r.cands[-2].marker:
            r.cands[-2].marker = b.marker
        b.marker = None
    return len(rows)


# ------------------------------------------------------------------ row analysis
def _mark_dates(line: Line):
    """Find dates in the line; remember which word indices belong to a date."""
    text = line.text
    offs, pos = [], 0
    for w in line.words:
        offs.append((pos, pos + len(w.t)))
        pos += len(w.t) + 1
    dates = find_dates(text)
    for s, e, _d in dates:
        for i, (ws, we) in enumerate(offs):
            if ws < e and we > s:
                line.date_word_idx.add(i)
    m = SERIAL_RE.match(text)
    serial_end = m.end() if m else 0
    if dates and dates[0][0] not in (0, serial_end):
        # allow up to two short id tokens before the date: "13 S28590109 26/08/2025 ..."
        pre = text[:dates[0][0]].split()
        if 1 <= len(pre) <= 2 and all(ID_TOKEN_RE.match(t) for t in pre):
            serial_end = dates[0][0]
    lead = []
    if dates and dates[0][0] in (0, serial_end):
        lead.append(dates[0])
        # second leading date (value date) directly after the first
        if len(dates) > 1 and dates[1][0] - dates[0][1] <= 2:
            lead.append(dates[1])
    line.anchor_dates = [d for _, _, d in lead]
    lead_end = lead[-1][1] if lead else 0
    return offs, serial_end if lead else 0, lead_end


def _ints_in_money_cols(cands: list[Cand], layout: dict | None) -> bool:
    """Bare-integer tail is real money when every number sits under a header money column and the last one
    under Balance (AA-rendered PDFs print round amounts without decimals: '500 500', '1 0')."""
    if not layout or len(cands) < 2 or len(cands) > 3:  # need amount + balance ('Page 1 of 24' footers have one)
        return False
    cols = [_assign_col(c, layout) for c in cands]
    return all(cols) and cols[-1] == "balance" and cols.count("balance") == 1


def _narr_after_start(words: list[W], start: int, layout: dict) -> int:
    """Narration column right of Balance (layout '_narr_after'): index of the date line's first narration word -
    the first word right of the Balance title that is not a decimal amount or a Dr/Cr marker (a bare '2022' /
    '692' there is text). len(words) when the line has no narration of its own."""
    ax = min(layout["_after_balance"][0], layout["balance"][1] + 5) - 2
    for k in range(start, len(words)):
        w = words[k]
        if w.x0 < ax:
            continue
        if MARKER_RE.match(w.t) and k > start and w.x0 - words[k - 1].x1 < 15 \
                and (parse_amount_token(words[k - 1].t) or (0, 0, False))[2]:
            continue  # '1,234.00 Cr': the balance's own marker; 'DR LAL PATHLABS' / 'CR ADJ ..' start the narration
        p = parse_amount_token(w.t)
        if p is None or not p[2]:
            return k
    return len(words)


def _split_tail(words: list[W], date_idx: set[int], start: int, layout: dict | None = None):
    """Scan from the right collecting amounts / markers; stop at narration or a date.
    layout is passed only for dated anchor rows: it lets a bare-integer tail through if it fits the columns."""
    cands: list[Cand] = []
    i = len(words) - 1
    if layout and layout.get("_narr_after"):
        i = _narr_after_start(words, start, layout) - 1  # 'SALARY FOR APRIL 2022' is text, not an amount
    elif layout and "_after_balance" in layout:
        # 'UPI' / 'IMPS' / '9999-CENTRAL OFF' in the Channel column after Balance; centred values can start left of
        # the Channel title, so anything that is not an amount or a Dr/Cr marker right of the Balance title counts
        ax = min(layout["_after_balance"][0], layout["balance"][1] + 5) - 2
        # Axis 'Init. Br' prints a bare branch code ('1572', '400') there: a digits-only word right of the Balance
        # title is never the balance when an amount with decimals sits left of it
        while (i >= start and words[i].x0 >= ax
               and ((parse_amount_token(words[i].t) is None and not MARKER_RE.match(words[i].t))
                    or (re.fullmatch(r"\d{1,6}", words[i].t) and i - 1 >= start
                        and words[i].x0 >= layout["_after_balance"][0] - 5  # under 'Init. Br', not the Balance cell
                        and (parse_amount_token(words[i - 1].t) or (0, 0, False))[2]))):
            i -= 1
    while i >= start and TAIL_JUNK_RE.match(words[i].t):
        i -= 1
    pending = None
    last_amt = None  # word index of the amount consumed last (the one right of the current word)
    while i >= start:
        if i in date_idx:
            break
        t = words[i].t
        if t.upper() in CURRENCY_TOKENS and last_amt == i + 1 and words[i + 1].x0 - words[i].x1 < 12:
            # 'INR 99,800.00' (Indian Bank) / '₹ 185.00' (Punjab & Sind) printed as two words: the currency prefix belongs to the amount on its right
            i -= 1
            continue
        mm = MARKER_RE.match(t)
        if mm:
            pending = "CR" if mm.group(1).lower() == "cr" else "DR"
            i -= 1
            continue
        if t in PLACEHOLDER_TOKENS:
            pending = None
            i -= 1
            continue
        p = parse_amount_token(t)
        if p is None:
            break
        if not p[2] and any(c.has_dec for c in cands) and re.fullmatch(r"\d{7,}", t):
            # amounts printed with decimals: a bare 7+ digit run is a ref / date code ending the narration
            # ('NACH RTN ACH DR MUTHOOT FINANCE LIMI 13032026 354.00'), not money
            break
        val, mk, has_dec, sign = p
        cands.insert(0, Cand(value=val, marker=mk or pending, sign=sign, x0=words[i].x0, x1=words[i].x1,
                             has_dec=has_dec))
        pending = None
        last_amt = i
        i -= 1
    if cands and not any(c.has_dec for c in cands) and layout and not _ints_in_money_cols(cands, layout):
        # '... CARD 4311 0400 0581 8416 379 0': drop leading integers that sit in the narration, not a money column
        k = 0
        while k < len(cands) - 1 and _assign_col(cands[k], layout) is None:
            k += 1
        if k and _ints_in_money_cols(cands[k:], layout):
            cands = cands[k:]
    if cands and not any(c.has_dec for c in cands) and not _ints_in_money_cols(cands, layout):
        return [], i  # bare integers only -> pin codes, ids, counts; not money
    return cands, i


def _classify(line: Line, layout: dict | None):
    offs, serial_end, lead_end = _mark_dates(line)
    is_anchor = bool(line.anchor_dates)
    start = 0
    if is_anchor:
        # skip serial + leading date words
        start = next((i for i, (ws, _we) in enumerate(offs) if ws >= lead_end), len(line.words))
    cands, stop = _split_tail(line.words, line.date_word_idx if not is_anchor else
                              {i for i in line.date_word_idx if i >= start}, start,
                              layout if is_anchor else None)
    rest = line.words[start:stop + 1] if stop >= start else []
    # drop serial number word if present (only for anchors)
    if is_anchor and serial_end:
        pass
    rest = [w for k, w in enumerate(line.words[start:stop + 1], start=start)
            if k not in line.date_word_idx or _date_inside(w.t)] if rest else []
    if is_anchor and rest and layout and "_branch_col" in layout:
        bx0, bx1 = layout["_branch_col"]
        rest = [w for w in rest if not (w.x0 >= bx0 and w.x1 <= bx1 and re.fullmatch(r"\d{3,6}", w.t))]
    for c in cands:
        c.col = _assign_col(c, layout)
    if rest:
        hint = DIR_WORDS.get(rest[-1].t.upper())
        if hint:
            line.dir_hint = hint
    if is_anchor and layout and layout.get("_narr_after"):
        rest = rest + [w for k, w in enumerate(line.words) if k >= _narr_after_start(line.words, start, layout)
                       and (k not in line.date_word_idx or _date_inside(w.t))]
    if not is_anchor and cands and layout and all(c.col is None for c in cands):
        # 'IN085720INR 3199.00' inside the description column: card-txn detail, not the row's amounts
        cands, rest = [], [w for k, w in enumerate(line.words) if k not in line.date_word_idx]
    line.cands, line.rest = cands, rest
    line.kind = "anchor" if is_anchor else ("amt" if cands else "narr")


def _date_inside(t: str) -> bool:
    """A narration word that merely ends in a date - SIB 'UPI/CRADJ621445348103/02-08-26', HDFC
    'REF-TECHIRCT1-804524609-13/3/26' - is text, not a date column: dropping it left the row without narration."""
    ds = find_dates(t)  # needs letters besides the date: '19-05-2026:00:00', '31/03/2025-29/06/2025' stay dates
    return bool(ds) and len(re.sub(r"[^A-Za-z]", "", t[:ds[0][0]] + t[ds[0][1]:])) >= 3


def _xcluster_layout(lines: list[Line]) -> dict | None:
    """Money columns from where the numbers sit, not from header words: on lines that start with a date and end in
    two or more decimal amounts, the rightmost amount's right edge is the balance column and the amount just before
    it the transaction column. Needs a tight cluster over >= 4 lines."""
    bal, amt = [], []
    for ln in lines:
        ds = find_dates(ln.text)
        if not ds or ds[0][0] > 8:
            continue
        got = []
        for w in reversed(ln.words):
            if MARKER_RE.match(w.t):
                continue
            p = parse_amount_token(w.t)
            if p is None or not p[2]:
                break
            got.append(w)
        if len(got) >= 2:
            bal.append(got[0])
            amt.append(got[1])
    if len(bal) < 4:
        return None
    if max(w.x1 for w in bal) - min(w.x1 for w in bal) > 25:
        return None
    return {"balance": (min(w.x0 for w in bal), max(w.x1 for w in bal)),
            "amount": (min(w.x0 for w in amt), max(w.x1 for w in amt))}


def _rows_amount_end(items_by_page: list[list[Line]], hdr_pages: set[int], stop_y: dict):
    """Rows cut at the lines that carry amount(s) + balance, not at date lines: a row is every line since the previous
    such line, up to and including this one. The date is carried from the last line that started with a date in the
    date column (banks that print the date once per day, on the first row of that day). Returns (rows, date_gaps)
    where date_gaps counts rows that had no date to carry and date lines that no row was cut after, and proof = one
    entry per table page: is every line's row certain? A line under a row's amount line is cut into the NEXT row, so a
    layout that prints the wrapped narration BELOW the amount line (SALARY text on the wrong debit while the chain is
    100%) is only told apart by spacing: rows must be set apart by a gap >= 1.4x every gap inside a row (single-line
    rows have nothing to misplace). Lines left after the last amount line of a page, or a row running over a page
    break, leave the page unproven."""
    raws: list[RawTxn] = []
    row_ln: list[Line] = []   # lines of the row being collected (proof only)
    prev_end: Line | None = None
    pg: dict[int, dict] = {}
    cur: tuple | None = None
    pending: list[str] = []
    gaps = 0
    dates_open = 0  # date lines seen since the last cut
    xs = sorted(round(a.words[min(a.date_word_idx)].x0 / 5) * 5 for items in items_by_page for a in items
                if a.kind == "anchor" and a.date_word_idx)
    col = next((x for x in xs if sum(abs(x - y) <= 5 for y in xs) >= 3), None)  # leftmost date column
    for pno, items in enumerate(items_by_page):
        if hdr_pages and pno not in hdr_pages:
            continue  # a page without the column titles (summary / cover page) is not the transaction table
        P = pg.setdefault(pno, {"wraps": [], "rows": [], "bad": None, "n": 0})
        for ln in items:
            if pno in stop_y and ln.top > stop_y[pno]:
                break  # under the page's closing / total line: summary lines, not rows
            # a date printed inside the narration (not in the date column) is text, never a row date
            stray = ln.kind == "anchor" and col is not None and ln.words[min(ln.date_word_idx)].x0 > col + 12
            if ln.kind == "anchor" and not stray:
                cur = (ln.anchor_dates[0], ln.anchor_dates[1] if len(ln.anchor_dates) > 1 else None)
                gaps += dates_open > 0  # the previous date line got no row of its own: carried dates may be shifted
                dates_open += 1
            body = " ".join(w.t for w in ln.words) if stray else " ".join(w.t for w in ln.rest) if ln.kind == "anchor"                 else " ".join(w.t for w in ln.words)
            if ln.cands and len(ln.cands) >= 2 and ln.kind in ("anchor", "amt"):
                if cur is None:
                    gaps += 1
                    pending = []
                    row_ln, prev_end = [], None
                    P["bad"] = P["bad"] or "amount line before any date"
                    continue
                row_ln.append(ln)
                if any(x.page != ln.page for x in row_ln):
                    P["bad"] = P["bad"] or "row runs over a page break"
                P["wraps"] += [b.top - a.top for a, b in zip(row_ln, row_ln[1:]) if a.page == b.page == ln.page]
                if prev_end is not None and prev_end.page == ln.page:
                    P["rows"].append(row_ln[0].top - prev_end.top)
                P["n"] += 1
                row_ln, prev_end = [], ln
                parts = pending + [" ".join(w.t for w in ln.rest) if not stray else body]
                raws.append(RawTxn(date=cur[0], value_date=cur[1], narration_parts=[" ".join(p for p in parts if p).strip()],
                                   cands=list(ln.cands), dir_hint=ln.dir_hint, page=ln.page, tops=[ln.top]))
                pending, dates_open = [], 0
            else:
                pending.append(body)
                row_ln.append(ln)
        if row_ln:
            # lines under the page's last amount line: page furniture (footer, page no.) when set farther off than
            # 1.4x the page's own row / wrap gap - dropped, not carried into the next page's first row. Anything
            # closer could be that row's wrapped narration (or its continuation): the page is not proven
            ref = max(P["wraps"]) if P["wraps"] else (min(P["rows"]) if P["rows"] else None)
            gap = row_ln[0].top - prev_end.top if prev_end is not None and prev_end.page == row_ln[0].page else None
            P["tail"] = (round(gap, 1) if gap is not None else None, len(row_ln))
            if ref is not None and gap is not None and gap >= 1.4 * ref and all(x.page == row_ln[0].page for x in row_ln):
                del pending[len(pending) - len(row_ln):]
                row_ln = []
            else:
                P["bad"] = P["bad"] or "lines left after the last amount line of the page"
    proof = []
    for pno, P in sorted(pg.items()):
        if not P["n"] and not P["bad"]:
            continue
        w, r = P["wraps"], P["rows"]
        if P["bad"]:
            ok, why = False, P["bad"]
        elif not w:
            ok, why = True, "single-line rows"
        elif not r:
            ok, why = False, "single row"
        elif min(r) >= 1.4 * max(w):
            ok, why = True, "spacing"
        else:
            ok, why = False, "rows not set apart by spacing"
        proof.append({"page": pno + 1, "proven": ok, "why": why, "rows": P["n"],
                      "wrap_max": round(max(w), 1) if w else None, "row_min": round(min(r), 1) if r else None,
                      "tail": P.get("tail")})
    return raws, gaps + dates_open, proof


def _demote_stray_anchors(anchors: list[Line]) -> list[Line]:
    """Lines that merely start with a date - '20JUL26 ELECTRO 02:01:59' card detail under the narration, the
    browser print header '10/11/2026, 12:16' - sit away from the date column. When nearly every anchor shares one
    date column, demote off-column anchors that don't carry amount + balance themselves."""
    if len(anchors) < 20:
        return anchors
    xs = [a.words[min(a.date_word_idx)].x0 for a in anchors]
    full = [x for a, x in zip(anchors, xs) if len(a.cands) >= 2]  # rows printing amount + balance on the date line
    if len(full) < 20:
        return anchors
    col = statistics.median(full)
    if sum(abs(x - col) <= 20 for x in full) < 0.9 * len(full):
        return anchors
    keep = []
    for a, x in zip(anchors, xs):
        if abs(x - col) > 20 and len(a.cands) <= 1:
            a.kind, a.cands, a.rest, a.anchor_dates = "narr", [], [], []
        else:
            keep.append(a)
    return keep


def _top_amount_for(ln: Line, anchor: Line, prev: Line | None, lh: float) -> bool:
    """Line `ln` carries only the Debit/Credit amount and the anchor right below it carries only the Balance:
    the amount cell is top-aligned while date + balance are vertically centred in a taller row. Needs header
    columns so we know which number is which; the balance chain then verifies the pairing row by row.
    The line must sit nearer to that anchor than to the previous one (BOB / IDFC print the amount a few points
    under their own date line - that amount belongs to the row above, not the next one)."""
    if not ln.cands or len(anchor.cands) != 1 or anchor.cands[0].col != "balance":
        return False
    if not all(c.col in ("debit", "credit", "amount") for c in ln.cands) or len(ln.cands) > 2:
        return False
    gap = anchor.mid - ln.mid
    if prev is not None and ln.mid - prev.mid <= gap:
        return False
    return 0 < gap <= 1.5 * lh


def _top_balance_for(ln: Line, anchor: Line, prev: Line | None, lh: float) -> bool:
    """Mirror case of _top_amount_for: the line above carries only the Balance (a wide lakh balance pushed up a
    line, Bank of Maharashtra '1,63,285.5') and the anchor below has its Debit/Credit amount but no balance."""
    if len(ln.cands) != 1 or ln.cands[0].col != "balance" or not anchor.cands:
        return False
    if any(c.col == "balance" for c in anchor.cands) or len(anchor.cands) > 2:
        return False
    if not all(c.col in ("debit", "credit", "amount") for c in anchor.cands):
        return False
    gap = anchor.mid - ln.mid
    if prev is not None and ln.mid - prev.mid <= gap:
        return False
    return 0 < gap <= 1.5 * lh


def _wrapped_amount_for(ln: Line, anchor: Line, nxt: Line | None, lh: float) -> bool:
    """UCO Bank: the date line holds only the Balance and the Withdrawal / Deposit amount wraps onto the row's
    next line (under the date line, nearer to it than to the next row). The balance chain verifies the pairing."""
    if len(anchor.cands) != 1 or anchor.cands[0].col != "balance" or not 1 <= len(ln.cands) <= 2:
        return False
    if not all(c.col in ("debit", "credit", "amount") for c in ln.cands) or not any(c.has_dec for c in ln.cands):
        return False
    gap = ln.mid - anchor.mid
    if nxt is not None and nxt.mid - ln.mid <= gap:
        return False
    return 0 < gap <= 1.5 * lh


# ------------------------------------------------------------------ drawn row grid
def _rows_from_grid(items: list[Line], anchors: list[Line], grid, raws: list[RawTxn], carry, money_x0,
                    narr_right: bool = False):
    """Build this page's rows from the drawn bands. A band without a date continues the row above it (or the
    previous page's last row, for bands before the first dated one); lines outside every band are page header /
    footer. Numbers on extra lines of a band complete that row's amount/balance when its date line lacks them.
    Returns the carry (last raw) for the next page."""
    bands, owner = grid
    starts = sorted(a.rest[0].x0 for a in anchors if a.rest)
    nx0 = starts[len(starts) // 2] if starts else None  # where narration text starts on this page
    # ... or right after the date column: a date line may hold only a ref no while the description sits centred
    # in the lines above/below it (TJSB: 'Txn Reference' on the date line, 'UPI/DR/..' above and below)
    ends = []
    for a in anchors:
        i = next((k for k, w in enumerate(a.words) if a.rest and w is a.rest[0]), 0)
        if i:
            ends.append(a.words[i - 1].x1)  # last date (or serial) word before the row's text
    ends.sort()
    if ends and nx0 is not None:
        nx0 = min(nx0, ends[len(ends) // 2] + 6)

    def in_narration_column(ln: Line) -> bool:
        # page margins ('about:blank 170/172') and repeated header words over the money columns ('Balance') are
        # not narration
        if nx0 is not None and ln.words[0].x0 < nx0 - 5:
            return False
        # (PNB prints the narration column right of Balance: the date lines' own text starts there)
        return (money_x0 is None or ln.words[0].x0 < money_x0
                or (narr_right and nx0 is not None and nx0 > money_x0))

    lead = sorted(a.words[0].x0 for a in anchors)
    date_x0 = min((a.words[min(a.date_word_idx)].x0 for a in anchors if a.date_word_idx), default=None)

    def wrapped_first_cell(ln: Line) -> Line | None:
        # AU Small Finance: the Bank Reference cell left of the date wraps ('INDBR2202609080' / '88') onto the same
        # line as the description's next line ('LENDING RUPPE/Loan Disbursal/...'): keep the description part
        if nx0 is None or date_x0 is None or not lead or abs(ln.words[0].x0 - lead[len(lead) // 2]) > 3:
            return None
        left = [w for w in ln.words if w.x0 < nx0 - 5]
        right = [w for w in ln.words if w.x0 >= nx0 - 5]
        if not left or not right or left[-1].x1 >= date_x0 or (money_x0 is not None and right[-1].x1 >= money_x0):
            return None
        return Line(ln.page, right)

    by_band: dict[int, list[Line]] = {}
    for ln in items:
        if ln.kind == "anchor":
            continue
        if ln.kind == "narr" and not in_narration_column(ln) and not _is_boilerplate(ln, money_x0):
            ln = wrapped_first_cell(ln) or ln
        k = _band_of(bands, ln.mid)
        if k is None or (ln.kind == "narr" and (_is_boilerplate(ln, money_x0) or not in_narration_column(ln))):
            continue
        by_band.setdefault(k, []).append(ln)
    order = sorted(owner)
    first = order[0]
    # continuation bands before the first dated band -> previous page's last row
    if carry is not None:
        raw = carry[0]
        for k in sorted(b for b in by_band if b < first):
            for ln in sorted(by_band[k], key=lambda l: l.top):
                if ln.kind == "narr":
                    raw.narration_parts[0] = (raw.narration_parts[0] + " " + ln.text).strip()
    last_raw = None
    for n, k in enumerate(order):
        a = owner[k]
        nxt = order[n + 1] if n + 1 < len(order) else len(bands)
        lines = [ln for b in range(k, nxt) for ln in by_band.get(b, [])]
        for ln in lines:
            if ln.kind == "amt":
                cols = {c.col for c in a.cands}
                # only numbers under a money column count; with decimals printed, a bare integer ('ID 141', a
                # wrapped ref digit '5') is narration, not money
                dec = any(c.has_dec for c in ln.cands)
                if money_x0 is None:  # no header columns known: the row's decimal amounts, only if its date line had none
                    money = [c for c in ln.cands if c.has_dec or not dec] if not a.cands else []
                    ok = bool(money)
                else:
                    money = [c for c in ln.cands if c.col and (c.has_dec or not dec)]
                    ok = bool(money) and all(c.col not in cols for c in money) and len({c.col for c in money}) == len(money)
                if ok:
                    a.cands = sorted(a.cands + money, key=lambda c: c.x0)
                    if ln.dir_hint and not a.dir_hint:
                        a.dir_hint = ln.dir_hint
                    ln.kind = "amt-used"
        if not a.cands:
            continue
        seq = sorted([a] + lines, key=lambda l: l.top)
        parts = []
        for ln in seq:
            ws = a.rest if ln is a else (ln.rest if ln.kind in ("amt", "amt-used") else ln.words)
            parts.append(" ".join(w.t for w in ws))
        tops = sorted(l.top for l in [a] + [ln for ln in lines if ln.kind == "amt-used"])
        last_raw = RawTxn(date=a.anchor_dates[0], value_date=a.anchor_dates[1] if len(a.anchor_dates) > 1 else None,
                          narration_parts=[" ".join(p for p in parts if p).strip()], cands=list(a.cands),
                          dir_hint=a.dir_hint, page=a.page, tops=tops)
        raws.append(last_raw)
    return (last_raw, 0.0) if last_raw is not None else None


def _row_separators(page) -> list[float]:
    """y positions of the row borders the bank itself drew: horizontal rules spanning most of the table, row
    stripes, and cell boxes whose top/bottom edges are shared by >= 2 cells (a row of cells). Sorted, de-duplicated.
    Sideways pages are skipped: their drawings are not in the parser's reading frame."""
    if reading_frame(page) is not None:
        return []
    W = page.rect.width
    ys: list[float] = []
    cell_edges: dict[float, int] = {}
    seg_len: dict[float, float] = {}  # rules drawn as one short segment per cell: add up the pieces at one height
    for dr in page.get_drawings():
        for it in dr["items"]:
            if it[0] == "l":
                a, b = it[1], it[2]
                if abs(a.y - b.y) < 1 and abs(a.x - b.x) >= 3:
                    k = round((a.y + b.y) * 2) / 4
                    seg_len[k] = seg_len.get(k, 0.0) + abs(a.x - b.x)
            elif it[0] in ("re", "qu"):
                r = it[1] if it[0] == "re" else it[1].rect
                if r.width >= 0.4 * W:
                    ys += [r.y0, r.y1]  # full-width stripe / rule drawn as a thin box
                elif r.width >= 15 and 2 <= r.height <= 120:
                    for y in (r.y0, r.y1):
                        k = round(y * 2) / 2
                        cell_edges[k] = cell_edges.get(k, 0) + 1
    ys += [y for y, n in cell_edges.items() if n >= 2]
    ys += [y for y, n in seg_len.items() if n >= 0.4 * W]
    out: list[float] = []
    for y in sorted(ys):
        if not out or y - out[-1] > 1.5:
            out.append(y)
    return out


def _grid_bands(seps: list[float], anchors: list[Line], items: list[Line] | None = None):
    """Bands between consecutive separators. Valid only when the drawn lines really are row borders:
    every anchor sits inside a band, no band holds two anchors, and no band without a date holds an amount under a
    money column (boxes drawn per text line - Bank of Maharashtra - put a row's balance in its own band).
    Returns (bands, anchor-by-band) or None."""
    if len(seps) < 4 or len(anchors) < 3:
        return None
    bands = list(zip(seps, seps[1:]))
    owner: dict[int, Line] = {}
    for a in anchors:
        k = next((i for i, (t, b) in enumerate(bands) if t <= a.mid <= b), None)
        if k is None or k in owner:
            return None
        owner[k] = a
    first, last = min(owner), max(owner)
    for ln in items or []:
        if ln.kind == "amt" and any(c.col for c in ln.cands):
            k = _band_of(bands, ln.mid)
            if k is not None and k > first and k not in owner:
                return None
    # row borders enclose a whole row, so between two dated rows there is no band holding text alone. Boxes drawn
    # around each text line / stripe (Bank of Maharashtra, HDFC, HSBC: a band per wrapped line, ~1 per row) say
    # nothing about which row a line belongs to
    text_only = {_band_of(bands, ln.mid) for ln in items or [] if ln.kind != "anchor"}
    if sum(1 for k in text_only if k is not None and first < k < last and k not in owner) > 0.25 * len(owner):
        return None
    return bands, owner


def _band_of(bands, y: float) -> int | None:
    return next((i for i, (t, b) in enumerate(bands) if t <= y <= b), None)


def _centred_narration(items: list[Line]) -> dict[int, Line]:
    """Description lines printed above their own date line. Between two date lines A and B the row boundary is the
    widest vertical gap: lines after it belong to B (Bank of Maharashtra centres a 3-line description on the date
    line, so one line sits above it). Only a clear gap counts (>= 1.4x every other gap of the run); evenly spaced
    wrap lines stay with the row above. Returns line id -> the anchor below it."""
    out: dict[int, Line] = {}
    k = 0
    while k < len(items):
        if items[k].kind != "anchor":
            k += 1
            continue
        j = k + 1
        while j < len(items) and items[j].kind == "narr":
            j += 1
        if j < len(items) and items[j].kind == "anchor" and j > k + 1 and items[j].page == items[k].page:
            seq = items[k:j + 1]
            gaps = [b.top - a.top for a, b in zip(seq, seq[1:])]
            g = max(range(len(gaps)), key=lambda i: gaps[i])
            others = gaps[:g] + gaps[g + 1:]
            if g < len(gaps) - 1 and others and gaps[g] >= 1.4 * max(others):
                for ln in seq[g + 1:-1]:
                    out[id(ln)] = items[j]
        k = j
    # a page laid out this way (most rows have a line above their date): the lines just above the page's first
    # date are its own, not a continuation of the previous page's last row
    anchors = [ln for ln in items if ln.kind == "anchor"]
    if anchors and len(set(map(id, out.values()))) >= 0.5 * (len(anchors) - 1) >= 1:
        first = items.index(anchors[0])
        lead = [ln for ln in items[:first] if ln.kind == "narr"]
        if lead and len(lead) == first and anchors[0].top - lead[-1].top <= 2 * (anchors[0].bottom - anchors[0].top):
            seq = lead + [anchors[0]]
            gaps = [b.top - a.top for a, b in zip(seq, seq[1:])]
            g = max(range(len(gaps)), key=lambda i: gaps[i]) if len(gaps) > 1 else -1
            others = gaps[:g] + gaps[g + 1:] if g >= 0 else []
            start = g + 1 if (g >= 0 and others and gaps[g] >= 1.4 * max(others)) else 0
            for ln in lead[start:]:
                out[id(ln)] = anchors[0]
    return out


def _centred_cells(items: list[Line], anchors: list[Line], lh: float):
    """Rows whose date + amounts sit in the vertical middle of a taller cell, the narration running from above the
    date line to below it (Canara: 'UPI/DR/<ref>/..' 3 lines above the date line, '.. Chq: <ref>' 3 lines below, no
    drawn row borders, rows evenly spaced). The default reading gives every row the bottom of its own narration plus
    the top of the next row's. Here the page's lines are tiled into one block per date line, each block exactly
    centred on its date word: the first block starts at the page's first table line, each next block right after the
    previous one, and a block's end is the one line whose bottom puts the block's middle on the date word's middle
    (within 1pt). Accepted only when every date line has its amounts, every line from the first block to the last is
    narration-column text in exactly one block, nothing but page furniture follows the last block, and at least half
    of the rows really have text above their date line. Returns (blocks: id(anchor) -> lines by top, rows with text
    above) or None. This decides the split only; it is not row-alignment proof for data_quality."""
    if len(anchors) < 1 or any(not a.cands or not a.date_word_idx for a in anchors):
        return None
    pos_of = {id(ln): k for k, ln in enumerate(items)}
    pos = [pos_of[id(a)] for a in anchors]
    tol = 1.0
    blocks: dict[int, list[Line]] = {}
    s, above_rows, col = 0, 0, []
    for i, a in enumerate(anchors):
        p = pos[i]
        lim = pos[i + 1] if i + 1 < len(anchors) else len(items)
        dw = a.words[min(a.date_word_idx)]
        top = min(ln.top for ln in items[s:p + 1])
        ends = [e for e in range(p, lim) if abs(top + max(ln.bottom for ln in items[s:e + 1]) - (dw.top + dw.bottom)) <= tol]
        if len(ends) != 1:
            return None
        blk = items[s:ends[0] + 1]
        if any(ln.kind != "narr" for ln in blk if ln is not a):
            return None
        col += [ln.words[0].x0 for ln in blk if ln is not a]
        blocks[id(a)] = blk
        above_rows += s < p
        s = ends[0] + 1
    if not col or above_rows < 0.5 * len(anchors):
        return None
    cx = statistics.median(col)
    dated = {id(a) for a in anchors}
    if any(abs(ln.words[0].x0 - cx) > 6 for blk in blocks.values() for ln in blk if id(ln) not in dated):
        return None
    last_bottom = max(ln.bottom for ln in blocks[id(anchors[-1])])
    for ln in items[s:]:  # after the last block: page footer only, not the head of a row cut off by the page end
        if abs(ln.words[0].x0 - cx) <= 6 and ln.top - last_bottom <= 2.6 * lh:
            return None
    return blocks, above_rows


REF_RUN_RE = re.compile(r"\d{10,}")


def _alignment_proof(items: list[Line], anchors: list[Line], attached: dict, carried: set, own: set, moved: bool,
                     money_x0: float | None):
    """Without drawn row borders, is it certain which row every line of this page belongs to? The structure must be
    unambiguous:
      - every date line prints its own text, also in the column the wrap lines use, and its row has amounts;
      - no line was moved to another row (centred descriptions, amounts printed above the date line);
      - every other line in the table is attached below a date line, not left of the text column (or continues
        the previous page's last row at the top of the page); text over the first date line is page header,
        farther off than any row gap.
    Then the rows are proven when they are set apart by their own spacing (JANA / SIB: wrap lines 12 pt apart,
    rows 18+ pt apart): the gap before each date line is >= 1.4x every gap inside a row. Evenly spaced pages (HDFC:
    one 17.2 pt pitch everywhere) cannot tell 'below this date' from 'above the next date' by geometry; they come
    back with why='even spacing' and the echo counts (a 10+ digit reference in the lines below a date line found on
    that date line / on the next one) for the file-level check in parse_pdf. Returns (proven, evidence)."""
    ev: dict = {"rows": len(anchors)}
    if moved:
        return False, {**ev, "why": "lines moved to the next date line (centred description / amount above date)"}
    bad = next((a for a in anchors if id(a) not in own or not a.cands), None)
    if bad is not None:
        return False, {**ev, "why": f"date line without its own text, or row without amounts: {bad.text[:50]!r}"}
    starts = sorted(a.rest[0].x0 for a in anchors)
    nx0 = starts[len(starts) // 2]
    # the wrap lines' column must also hold text on every date line, else a date line carries only a ref no and its
    # description sits in the lines around it (Bank of Maharashtra, TJSB)
    conts = sorted(ln.words[0].x0 for ls in attached.values() for ln in ls if ln.kind == "narr")
    if conts:
        cx = conts[len(conts) // 2]
        bad = next((a for a in anchors if not any(abs(w.x0 - cx) <= 6 for w in a.rest)), None)
        if bad is not None:
            return False, {**ev, "why": f"date line without text in the narration column: {bad.text[:50]!r}"}
    first, last = anchors[0], anchors[-1]
    placed = {id(ln) for ls in attached.values() for ln in ls} | carried
    above_first: list[Line] = []  # text left unplaced over the first date line: page header, or that row's own text?
    for ln in items:
        if ln.kind == "anchor":
            continue
        if _is_boilerplate(ln, money_x0):
            if first.top < ln.top < last.top:  # dropped from its row and ends attachment: not provable
                return False, {**ev, "why": f"fine-print line between date lines: {ln.text[:40]!r}"}
            continue
        if id(ln) in placed:
            # SIB prints a wrapped row's amounts on its second line: fine, as long as they sit under their own date
            if ln.kind != "narr" and not any(ln in attached[id(a)] and a.top < ln.top for a in anchors):
                return False, {**ev, "why": f"amount line moved up to a date line below it: {ln.text[:40]!r}"}
            if ln.words[0].x0 < nx0 - 6:
                return False, {**ev, "why": f"continuation left of the text column: {ln.text[:40]!r}"}
        elif first.top < ln.top < last.top:
            return False, {**ev, "why": f"line between date lines left unplaced: {ln.text[:40]!r}"}
        elif ln.top < first.top and ln.kind == "narr" and ln.words[0].x0 >= nx0 - 6 and (
                money_x0 is None or ln.words[0].x0 < money_x0):
            above_first.append(ln)
    wraps: list[float] = []
    rows: list[float] = []
    lead = sorted((ln for ln in items if id(ln) in carried), key=lambda l: l.top)
    if lead:
        wraps += [b.top - a.top for a, b in zip(lead, lead[1:])]
        rows.append(first.top - lead[-1].top)
    echo_own = echo_next = 0
    for i, a in enumerate(anchors):
        below = sorted(attached[id(a)], key=lambda l: l.top)
        seq = [a] + below
        wraps += [y.top - x.top for x, y in zip(seq, seq[1:])]
        if i + 1 < len(anchors):
            rows.append(anchors[i + 1].top - seq[-1].top)
        refs = REF_RUN_RE.findall(" ".join(ln.text for ln in below))
        if refs:
            mine = REF_RUN_RE.findall(a.text)
            nxt = REF_RUN_RE.findall(anchors[i + 1].text) if i + 1 < len(anchors) else []
            hit = lambda runs: any(x in r or r in x for x in refs for r in runs)
            o, n = hit(mine), hit(nxt)
            echo_own += o and not n
            echo_next += n and not o
    ev.update(wrap_max=round(max(wraps), 1) if wraps else None, row_min=round(min(rows), 1) if rows else None,
              multiline=sum(1 for a in anchors if attached[id(a)]), lead=len(lead), echo_own=echo_own,
              echo_next=echo_next)
    if above_first:
        # page header block (HDFC prints the branch address over every page): farther off than any row gap
        gap = first.top - max(ln.top for ln in above_first)
        need = max([1.4 * max(wraps)]) if wraps else 0.0
        if rows:
            need = max(need, min(rows))
        if not (wraps or rows) or gap < need:
            return False, {**ev, "why": f"text {gap:.0f}pt above the first date line: {above_first[-1].text[:40]!r}"}
    if wraps and not rows:
        return False, {**ev, "why": "single row"}  # judged in parse_pdf against the row gaps of the other pages
    if wraps and min(rows) < 1.4 * max(wraps):
        return False, {**ev, "why": "even spacing" if min(rows) >= 0.9 * max(wraps) else
                       "rows not set apart by spacing"}
    return True, {**ev, "why": "spacing"}


def _money_x0(layout: dict | None) -> float | None:
    xs = [layout[r][0] for r in ("debit", "credit", "amount", "balance") if layout and r in layout]
    return min(xs) if xs else None


def _is_boilerplate(ln: Line, money_x0: float | None) -> bool:
    """A paragraph of fine print (RBI / GST notices after the last row): many words running into the money columns."""
    n = len(ln.words)
    return n >= 14 or (n >= 10 and money_x0 is not None and ln.words[-1].x1 > money_x0)


def _summary_roles(line: Line) -> list[str]:
    toks = [re.sub(r"[^a-z]", "", w.t.lower()) for w in line.words]
    roles = []
    for k, t in enumerate(toks):
        nxt = toks[k + 1] if k + 1 < len(toks) else ""
        prv = toks[k - 1] if k else ""
        r = None
        if t == "opening":
            r = "opening"
        elif t == "closing":
            r = "closing"
        elif t == "count":
            r = "dr_count" if prv in ("dr", "debit", "debits") else ("cr_count" if prv in ("cr", "credit", "credits") else None)
        elif t in ("debit", "debits", "withdrawal", "withdrawals", "dr") and nxt != "count":
            r = "debits"
        elif t in ("credit", "credits", "deposit", "deposits", "cr") and nxt != "count":
            r = "credits"
        if r and (not roles or roles[-1] != r):
            roles.append(r)
    return roles


def _label_values(line: Line) -> dict | None:
    """'Total Debit: 690677.0 Total Credit: 872875.0' on one line (IOB): each total is the number after its own
    label, not the line's last number. None unless both labels are on the line, each directly followed by a number."""
    out = {}
    for key, rx in (("debits", r"^(debit|withdrawal)s?:?$"), ("credits", r"^(credit|deposit)s?:?$")):
        k = next((i for i, w in enumerate(line.words[1:], 1)
                  if re.match(rx, w.t, re.I) and line.words[i - 1].t.lower() == "total"), None)
        p = parse_amount_token(line.words[k + 1].t) if k is not None and k + 1 < len(line.words) else None
        if not p:
            return None
        out[key] = p[0]
    return out


def _numbers(line: Line) -> list[float]:
    out = []
    for w in line.words:
        p = parse_amount_token(w.t)
        if p:
            out.append(p[0] * (-1 if p[3] == "-" or p[1] == "DR" else 1))
        elif w.t not in PLACEHOLDER_TOKENS and not MARKER_RE.match(w.t):
            return []
    return out


def _broken_token(prev: list[W], nxt: list[W], lines: list[list[W]]) -> bool:
    """UCO wraps a token wider than the description column mid-word ('MPAY/IMPSI/TRTR/6167../ICIC/SW' +
    'ASTIKFINAN'): the line above is one word filling the column to its right edge (the widest line starting where
    it starts) and the next line starts in the same column. A line of several words broke at a real space."""
    if len(prev) != 1 or not nxt or abs(nxt[0].x0 - prev[0].x0) > 3 or prev[0].x1 - prev[0].x0 <= 120:
        return False
    w = prev[0]
    edge = max(l[-1].x1 for l in lines if abs(l[0].x0 - w.x0) <= 3)
    return edge - w.x1 < 0.6 * (w.bottom - w.top)


def _wrap_lines(rows: list[list[list[W]]]) -> list[list[W]] | None:
    """rows: the narration lines of each row on a page. The page's narration lines when it breaks over-wide tokens
    mid-word: >= 5 single-word lines continued below, >= 90% of them ending at the column edge (UCO 998 of 1007).
    Columns that wrap at spaces end such lines anywhere (SIB 24 of 174: 'MOHAMED' + 'MOHIDEEN' stays two words)."""
    lines = [l for r in rows for l in r if l]
    pairs = [(p, n) for r in rows for p, n in zip(r, r[1:])
             if len(p) == 1 and n and abs(n[0].x0 - p[0].x0) <= 3 and p[0].x1 - p[0].x0 > 120]
    if len(pairs) < 5:
        return None
    near = sum(_broken_token(p, n, lines) for p, n in pairs)
    return lines if near >= 0.9 * len(pairs) else None


# ------------------------------------------------------------------ rows cut at their amount line (HSBC e-statement)
TURNOVER_RE = re.compile(r"^transaction\s+turnover\b", re.I)
TXN_COUNT_RE = re.compile(r"^transaction\s+count\b", re.I)


def _closing_amount_rows(items_by_page: list[list[Line]], hdr_pages: set[int], layout: dict | None,
                         bf_lines: list[tuple[int, Line]], close_marks: list[tuple[int, float]]) -> dict | None:
    """HSBC e-statement (OpenText Exstream, 'Date | Transaction Details | Deposits | Withdrawals | Balance'): the date
    is printed once per day, on the first line of that day's first row, and a row's amount + balance sit on its LAST
    line. Rows of the same day carry no date, and a row may start on one page and end on the next:
        24Sep2026  BALANCE BROUGHT FORWARD                                     58.80
        25Sep2026  CREDIT CARD PAYMENT
                   3561 9410 0138 3348                           58.80          0.00
        27Sep2026  UPI20260927001022846
                   216659404201
                   <payee>                          20.00                      20.00
                   IDFB6270M3243109                                 <- next row: same day, no date
                   ...
                   2026/09/27 180334 IDFB6270M3243109  10,300.80            10,320.80
                   IMPS
                   627213017332
                   Balance Carried Forward                                 85,173.80   <- page ends mid-row
    Read as date-anchored rows, every undated row merges into the dated one above it (23 rows for 85). Here a row is
    every table line after the previous row's amount line up to and including its own, dated by the last date line.
    Used only when the whole table tiles that way, else None and the caller's reading stands:
      - Deposits / Withdrawals / Balance titles; row lines only on pages printing them, no other page in between;
      - a date line (date word first, in the file's date column) opens a row: nothing is pending above it, and the
        first row starts with one;
      - every line with numbers under the money columns is exactly amount + balance (with decimals): a row's end;
      - every other line is text in the narration column, left of the money columns (header residue printed under
        the money titles - '(DR=Debit)', 'INR' - is skipped);
      - no 'Closing Balance' line before the last row (a second account / table).
    Lines after the last row are the trailer: 'Transaction Turnover <deposits> <withdrawals>' and 'Transaction Count
    <n deposits> <n withdrawals>' under their columns are the printed summary. Returns {'raws', 'pages', 'summary',
    'opening'}. Not a row-alignment proof (no drawn borders, evenly spaced lines)."""
    if not layout or not {"debit", "credit", "balance"} <= set(layout):
        return None
    money_x0 = _money_x0(layout)
    seq = [(p, ln) for p in sorted(hdr_pages) if p < len(items_by_page) for ln in items_by_page[p]]
    dated = [ln for _, ln in seq if ln.kind == "anchor" and 0 in ln.date_word_idx]
    if len(dated) < 3:
        return None
    dcol = statistics.median(ln.words[0].x0 for ln in dated)
    is_date = [ln.kind == "anchor" and 0 in ln.date_word_idx and abs(ln.words[0].x0 - dcol) <= 20 for _, ln in seq]
    starts = [ln.rest[0].x0 for (_, ln), d in zip(seq, is_date) if d and ln.rest]
    if not starts:
        return None
    nx = statistics.median(starts)  # narration column

    def money(ln: Line) -> list[Cand]:
        return [c for c in ln.cands if c.col is not None]

    def row_end(ms: list[Cand]) -> bool:
        return (len(ms) == 2 and ms[0].col in ("debit", "credit") and ms[1].col == "balance"
                and ms[0].x1 < ms[1].x0 and all(c.has_dec for c in ms))

    kinds = []
    for (_, ln), d in zip(seq, is_date):
        ms = money(ln)
        if d:
            k = "date" if not ms else ("date-end" if row_end(ms) else "bad")
        elif ms:
            k = "end" if row_end(ms) else "bad"
        elif ln.words[0].x0 >= money_x0 - 2:
            k = "skip"  # header residue under the money titles: '(DR=Debit)', 'INR'
        else:
            k = "narr"
        kinds.append(k)
    ends = [i for i, k in enumerate(kinds) if k in ("end", "date-end")]
    if len(ends) < 3:
        return None
    last = ends[-1]
    lp, lln = seq[last]
    if any((p, top) < (lp, lln.top) for p, top in close_marks):
        return None  # 'Closing Balance' before the last row: more than one account / table
    raws: list[RawTxn] = []
    pending: list[str] = []
    cur = None
    first = None
    row_pages: set[int] = set()
    opened = False
    prev = None  # (page, top, closed a row) of the line before
    in_gaps: list[float] = []
    cut_gaps: list[float] = []
    undated = 0  # rows opening without a date line: the reason for this layout (date printed once per day)
    for i in range(last + 1):
        p, ln = seq[i]
        k = kinds[i]
        if k == "skip":
            continue
        if k == "bad":
            return None
        if k in ("date", "date-end"):
            if pending:
                return None  # a date line inside a row: not this layout
            cur = ln.anchor_dates[0]
            opened = True
            words = ln.rest
        else:
            if cur is None:
                return None  # text / amounts before the first date line
            if not opened:
                undated += 1
            ms = money(ln)
            words = [w for w in ln.words if not ms or w.x1 <= ms[0].x0]
        if words and (abs(words[0].x0 - nx) > 6 or words[-1].x1 >= money_x0):
            return None  # text outside the narration column
        if prev and prev[0] == p:
            (cut_gaps if prev[2] else in_gaps).append(ln.top - prev[1])
        prev = (p, ln.top, k in ("end", "date-end"))
        opened = True
        if first is None:
            first = (p, ln.top)
        if words:
            pending.append(" ".join(w.t for w in words))
        row_pages.add(p)
        if k in ("end", "date-end"):
            raws.append(RawTxn(date=cur, narration_parts=[" ".join(pending)], cands=list(money(ln)), page=ln.page,
                               tops=[ln.top]))
            pending = []
            opened = False
    if cut_gaps and in_gaps and sum(g > statistics.median(cut_gaps) + 1.5 for g in in_gaps) >= max(2, 0.2 * len(raws)):
        return None  # rows set apart by a wider gap inside the cut rows: the cut is off by a line
    if not undated:
        return None  # every row dated: amounts may sit on a row's first line, the date below it - not this layout
    lo = min(row_pages)
    if any(p not in hdr_pages and any(ln.kind in ("anchor", "amt") for ln in items_by_page[p])
           for p in range(lo, len(items_by_page))):
        return None  # a later page without the column titles carries rows (header on page 1 only): never drop it
    summ: dict = {}
    for i in range(last + 1, len(seq)):
        p, ln = seq[i]
        ms = money(ln)
        if (is_date[i] or ms) and not (TURNOVER_RE.match(ln.text) or SKIP_RE.search(ln.text)):
            return None  # a row after the last amount + balance line (no balance, odd numbers): never drop it
        if TURNOVER_RE.match(ln.text) and len(ms) == 2 and {c.col for c in ms} == {"credit", "debit"} \
                and all(c.has_dec for c in ms):
            summ.update({("credits" if c.col == "credit" else "debits"): c.value for c in ms})
        elif TXN_COUNT_RE.match(ln.text) and len(ln.words) == 4 and all(re.fullmatch(r"\d{1,6}", w.t) for w in ln.words[2:]):
            n = {}
            for w in ln.words[2:]:
                col = _assign_col(Cand(value=float(w.t), x0=w.x0, x1=w.x1), layout)
                if col in ("credit", "debit"):
                    n["cr_count" if col == "credit" else "dr_count"] = int(w.t)
            if len(n) == 2:
                summ.update(n)
    opening = None
    bf = [ln for p, ln in bf_lines if (p, ln.top) < first]
    if bf and bf[-1].cands[-1].col == "balance":
        c = bf[-1].cands[-1]
        opening = -c.value if (c.marker == "DR" or c.sign == "-") else c.value
    return {"raws": raws, "pages": sorted(row_pages), "summary": summ, "opening": opening}


# ------------------------------------------------------------------ main
def parse_pdf(path: str, password: str | None = None, doc: fitz.Document | None = None) -> ParseResult:
    own = doc is None
    if doc is None:
        doc = fitz.open(path)
        if doc.needs_pass and not doc.authenticate(password or ""):
            raise PermissionError("password")
    pages_lines: list[list[Line]] = []
    page_seps: list[list[float]] = []
    text_chars = 0
    opts = OPTS.get()
    cache = opts.cache
    for pno in range(doc.page_count):
        check_deadline()  # self-heal strategies only
        if cache is not None and pno in cache:
            words, seps = cache[pno]
        else:
            words, seps = _page_words(doc[pno]), _row_separators(doc[pno])
            if cache is not None:
                cache[pno] = (words, seps)
        text_chars += sum(len(w.t) for w in words)
        pages_lines.append(_fix_wrapped_dates(_group_lines(pno, words)))
        page_seps.append(seps)
    _fix_yearless_dates(pages_lines)
    res = ParseResult(raw=[], pages=doc.page_count, text_chars=text_chars, method="pdf-text")
    if own:
        doc.close()

    layout = None
    header_lines: list[str] = []
    page1 = pages_lines[0] if pages_lines else []
    res.page1_text = "\n".join(l.text for l in page1)
    anchors_all: list[Line] = []
    items_by_page: list[list[Line]] = []
    summary: dict = {}
    summary_blocks = 0
    grand_totals: list[list[Cand]] = []
    seen_header = False
    diag: dict = {}  # self-heal evidence (selfheal.py): money lines, header words, trailing lines
    hdr_set = {p for p, lines in enumerate(pages_lines)
               if any(_detect_header(ln) or (k + 1 < len(lines) and _detect_header_2line(ln, lines[k + 1]))
                      for k, ln in enumerate(lines))} if opts.row_cut == "amount_end" else set()
    hdr_pages: set[int] = set()  # pages printing the column titles (for _closing_amount_rows)
    bf_lines: list[tuple[int, Line]] = []  # in-table 'Balance Brought Forward <bal>' lines
    close_marks: list[tuple[int, float]] = []  # in-table 'Closing Balance' lines (page, top)

    for pno, lines in enumerate(pages_lines):
        hdr_idx = None
        for k, ln in enumerate(lines):
            lay = _detect_header(ln)
            if lay:
                hdr_idx, layout = k, lay
                seen_header = True
                break
            lay = _detect_header_2line(ln, lines[k + 1]) if k + 1 < len(lines) else None
            if lay:
                hdr_idx, layout = k + 1, lay  # body starts after the second header line
                seen_header = True
                break
        if opts.col_roles == "xcluster":
            layout = _xcluster_layout(lines) or layout  # self-heal: money columns from the numbers' own x positions
        if hdr_idx is not None:
            hdr_pages.add(pno)
            if "hdr" not in diag:
                diag["hdr"] = {"words": [w.t.lower() for w in lines[hdr_idx].words],
                               "x": [round(w.x0 / 10) * 10 for w in lines[hdr_idx].words]}
        if pno == 0:
            header_lines = [l.text for l in (lines[:hdr_idx] if hdr_idx is not None else lines[:45])]
        items: list[Line] = []
        skip_next = False
        summary_roles = None
        stop_top = None  # y of the first closing / total line under the rows of this page (self-heal amount_end)
        for k, ln in enumerate(lines):
            if hdr_idx is not None and k == hdr_idx:
                continue
            in_body = hdr_idx is None or k > hdr_idx
            if skip_next:
                skip_next = False
                nums = _numbers(ln)
                if summary_roles and len(nums) == len(summary_roles):
                    summary_blocks += 1
                    if summary_blocks == 1:
                        summary.update(dict(zip(summary_roles, nums)))
                    continue
            low = ln.text.lower()
            if "opening" in low and "closing" in low:
                roles = _summary_roles(ln)
                if len(roles) >= 3:
                    summary_roles, skip_next = roles, True
                    # values printed on the line above their labels (Airtel '₹10597.50 ₹353.54 ₹433330.37 ..' over
                    # 'Opening Balance Closing Balance Total Credit Total Debit'): only when no values follow the
                    # labels, the line above is nothing but those values, and they balance (opening + cr - dr = closing)
                    up = _numbers(lines[k - 1]) if k and items and items[-1] is lines[k - 1] else []
                    s = dict(zip(roles, up)) if len(up) == len(roles) else {}
                    if (set(s) == {"opening", "closing", "credits", "debits"}
                            and (k + 1 >= len(lines) or len(_numbers(lines[k + 1])) != len(roles))
                            and abs(s["opening"] + s["credits"] - s["debits"] - s["closing"]) < 0.01):
                        skip_next = False
                        items.pop()
                        summary_blocks += 1
                        if summary_blocks == 1:
                            summary.update(s)
                continue
            _classify(ln, layout)
            if OPENING_RE.search(ln.text):
                if ln.cands and in_body and hdr_idx is not None:
                    bf_lines.append((pno, ln))
                if ln.cands and in_body and ln.cands[-1].has_dec:
                    c = ln.cands[-1]
                    diag.setdefault("bf", []).append((pno, -c.value if (c.marker == "DR" or c.sign == "-") else c.value))
                if ln.cands and res.opening_balance is None and not anchors_all and not any(i.kind == "anchor" for i in items):
                    c = ln.cands[-1]
                    res.opening_balance = -c.value if (c.marker == "DR" or c.sign == "-") else c.value
                elif not ln.cands and not in_body and res.opening_balance is None and not anchors_all:
                    # key-value header with a second field after the amount (YES Bank
                    # 'Opening Balance : 0.00 ACCOUNT TYPE : CURRENT ACCOUNT'): the amount follows the label
                    m = re.search(r"\bopening\s+balance\s*:\s*(\S+)(?:\s+(cr|dr)\b)?", ln.text, re.I)
                    p = parse_amount_token(m.group(1)) if m else None
                    if p and p[2]:
                        neg = p[1] == "DR" or p[3] == "-" or (m.group(2) or "").lower() == "dr"
                        res.opening_balance = -p[0] if neg else p[0]
                continue
            if not in_body:
                continue
            if ln.kind == "anchor" and not ln.cands and PAGE_FOOTER_RE.search(ln.text):
                continue  # RBL print footer starting with the print date: page furniture, not a row (nor a table page)
            if ln.kind != "anchor" and SKIP_RE.search(ln.text):
                if stop_top is None and items:
                    stop_top = ln.top
                    diag.setdefault("stop_y", {})[pno] = ln.top
                tot = summary
                if opts.row_cut == "amount_end" and hdr_set and pno not in hdr_set:
                    continue  # self-heal: totals printed on a page without the column titles are another table's
                m = re.search(r"closing\s*bal", ln.text, re.I)
                if m and hdr_idx is not None:
                    close_marks.append((pno, ln.top))
                if m and ln.cands:
                    tot.setdefault("closing", ln.cands[-1].value)
                both = _label_values(ln)
                for key, rx in (("debits", r"total\s*(debit|withdrawal)"), ("credits", r"total\s*(credit|deposit)")):
                    if re.search(rx, ln.text, re.I) and ln.cands:
                        # a statement's total debits / credits is never negative: 'TOTAL DEPOSITS -534.31' on an
                        # ICICI cover page is the relationship summary (savings + FD + RD balances), not this table
                        neg = re.search(rx + r"[A-Za-z]*\s*:?\s*(-)?[\d,]+(?:\.\d+)?\s*(DR\b)?", ln.text, re.I)
                        if neg and (neg.group(neg.re.groups - 1) or neg.group(neg.re.groups)) and not both:
                            continue  # the number right after this label is negative / DR
                        tot.setdefault(key, both[key] if both else ln.cands[-1].value)
                if (re.search(r"\btotal\b", ln.text, re.I) and not re.search(r"page\s*total", ln.text, re.I)
                        and [c.col for c in ln.cands] == ["debit", "credit", "balance"]):
                    grand_totals.append(ln.cands)  # 'Total: <debits> <credits> <closing>' under the money columns
                continue
            if stop_top is not None and ln.top > stop_top:
                diag.setdefault("tail", []).append(ln.text)
            items.append(ln)
            if ln.kind == "anchor":
                anchors_all.append(ln)
            if ln.cands and ln.cands[-1].has_dec and ln.kind in ("anchor", "amt"):
                diag.setdefault("money_lines", []).append((pno, round(ln.top, 1), len(ln.cands), ln.kind))
        items_by_page.append(items)

    if len(grand_totals) == 1 and not summary_blocks:  # one grand-total line only; per-page totals are ambiguous
        d, c, b = grand_totals[0]
        summary.setdefault("debits", d.value)
        summary.setdefault("credits", c.value)
        summary.setdefault("closing", -b.value if (b.marker == "DR" or b.sign == "-") else b.value)
    res.header_text = "\n".join(header_lines)
    if not anchors_all:
        res.warnings.append("No dated transaction rows found in the text layer.")
        return res

    anchors_all = _demote_stray_anchors(anchors_all)
    own_cands = sum(1 for a in anchors_all if a.cands)
    mode = "below" if own_cands >= 0.6 * len(anchors_all) else "centered"
    if opts.force_mode in ("below", "centered"):
        mode = opts.force_mode
    res.diag = diag
    diag["hdr_pages"] = sorted(hdr_pages)
    diag["anchors"] = len(anchors_all)
    diag["default_mode"] = "below" if own_cands >= 0.6 * len(anchors_all) else "centered"
    res.method = f"pdf-text/{mode}" + ("/header" if seen_header else "/no-header")
    ca = _closing_amount_rows(items_by_page, hdr_pages, layout, bf_lines, close_marks) if mode == "centered" else None
    if ca is not None:
        # HSBC e-statement: date printed once per day, each row's amount + balance on its last line (see there)
        if res.opening_balance is None:
            res.opening_balance = ca["opening"]
        summary.update(ca["summary"])  # over a cover page's 'Total Deposits and Investments' (portfolio, not rows)
        res.method = "pdf-text/closing-amount/header"
        res.page_proof = [{"page": p + 1, "proven": False, "rows": sum(1 for r in ca["raws"] if r.page == p),
                           "why": "rows cut at their amount line (date once per day): no drawn borders, even spacing"}
                          for p in ca["pages"]]
        _type_markers_after_balance(ca["raws"], layout, res.opening_balance)
        res.raw = ca["raws"]
        res.table_pages = len(ca["pages"])
        if summary_blocks > 1:
            res.warnings.append(f"File contains {summary_blocks} statement summaries - looks like several statements "
                                "merged into one PDF; printed-summary check skipped.")
        elif summary:
            res.printed_summary = summary
        return res

    raws: list[RawTxn] = []
    money_x0 = _money_x0(layout)
    carry: tuple[RawTxn, float] | None = None  # last row of the previous page + its narration x0
    grid_pages = aligned_pages = cell_pages = 0
    last_table_page = max(p for p, it in enumerate(items_by_page) if any(ln.kind == "anchor" for ln in it))

    def line_height(items):
        gaps = [b.top - a.top for a, b in zip(items, items[1:]) if b.top > a.top]
        return statistics.median(gaps) if gaps else 12.0

    # date line centred on a taller cell (Canara): tile the lines around each date line, see _centred_cells. Used only
    # when most of the file's rows are laid out that way
    cells: dict[int, tuple] = {}
    if mode == "below":
        for pno, items in enumerate(items_by_page):
            pa = [ln for ln in items if ln.kind == "anchor"]
            t = _centred_cells(items, pa, line_height(items)) if pa else None
            if t is not None:
                cells[pno] = t
        if sum(t[1] for t in cells.values()) < max(3, 0.5 * len(anchors_all)):
            cells = {}
    if opts.row_cut == "amount_end":
        raws, date_gaps, ae_proof = _rows_amount_end(items_by_page, hdr_pages, diag.get("stop_y", {}))
        diag["date_gaps"] = date_gaps
        diag["alignment"] = ae_proof
        diag["alignment_ok"] = bool(ae_proof) and all(x["proven"] for x in ae_proof)
        if res.opening_balance is None and diag.get("bf") and hdr_pages:
            # the first 'balance brought forward' line on the first table page is the opening balance
            first = min(hdr_pages)
            bf = [b for p, b in diag["bf"] if p == first]
            if bf:
                res.opening_balance = bf[0]
    for pno, items in enumerate(items_by_page if opts.row_cut != "amount_end" else []):
        check_deadline()
        if not items:
            continue
        lh = line_height(items)
        page_anchors = [ln for ln in items if ln.kind == "anchor"]
        if not page_anchors:
            # the last row may wrap onto a trailing page without date lines (HDFC: between the page header and
            # 'STATEMENT SUMMARY'): its narration-column lines continue that row, up to the summary / fine print
            # Only the page right after the last table page, and only one unbroken block: once the first line is
            # taken, the first fine print / number / other-column line or a gap ends it (a legend page whose lines
            # happen to start at the narration column is not read)
            if carry is not None and mode == "below" and pno == last_table_page + 1:
                raw, nx0 = carry
                prev_top = None
                for ln in items:
                    if re.search(r"summary", ln.text, re.I):
                        break  # the summary block: the row has ended
                    fits = (ln.kind == "narr" and not _is_boilerplate(ln, money_x0) and abs(ln.words[0].x0 - nx0) <= 5
                            and (money_x0 is None or ln.words[-1].x1 < money_x0))
                    if prev_top is None:
                        if not fits:
                            continue  # page header ('OD Limit : 0.00', 'From : .. To :') before the continuation
                    elif not fits or ln.top - prev_top > 2.6 * lh:
                        break
                    raw.narration_parts[0] = (raw.narration_parts[0] + " " + ln.text).strip()
                    prev_top = ln.top
            carry = None
            continue
        grid = _grid_bands(page_seps[pno], page_anchors, items) if pno < len(page_seps) else None
        if grid is not None:
            # the bank drew the row borders: every line belongs to the transaction whose row box it sits in
            grid_pages += 1
            carry = _rows_from_grid(items, page_anchors, grid, raws, carry, money_x0,
                                    narr_right=bool(layout and layout.get("_narr_after")))
            continue
        if pno in cells:
            blocks, n_above = cells[pno]
            seqs = [[ln.rest if ln is a else ln.words for ln in blocks[id(a)]] for a in page_anchors]
            wrap_lines = _wrap_lines(seqs)
            echo = 0
            for a, seq in zip(page_anchors, seqs):
                parts: list[str] = []
                prev_ws = None
                for ws_ in seq:
                    if not ws_:
                        continue
                    if wrap_lines and prev_ws and parts[-1] and _broken_token(prev_ws, ws_, wrap_lines):
                        parts[-1] += ws_[0].t
                        parts.append(" ".join(w.t for w in ws_[1:]))
                    else:
                        parts.append(" ".join(w.t for w in ws_))
                    prev_ws = ws_
                blk = blocks[id(a)]
                # evidence only: the reference on the cell's first line printed again further down the same cell
                # (Canara 'UPI/DR/<ref>/..' .. 'Chq: <ref>')
                refs = REF_RUN_RE.findall(blk[0].text) if blk[0] is not a else []
                echo += any(r in ln.text for r in refs for ln in blk[1:])
                raws.append(RawTxn(date=a.anchor_dates[0], value_date=a.anchor_dates[1] if len(a.anchor_dates) > 1 else None,
                                   narration_parts=[" ".join(p for p in parts if p).strip()], cands=list(a.cands),
                                   dir_hint=a.dir_hint, page=a.page, tops=[a.top]))
            cell_pages += 1
            # the split is decided by geometry, but centred cells are not one of the row-alignment proofs (BoM / TJSB
            # centred text stays REVIEW): the page stays unproven
            res.page_proof.append({"page": pno + 1, "proven": False, "rows": len(page_anchors),
                                   "why": "centred cells (date line centred on its cell; not an alignment proof)",
                                   "centred_rows": n_above, "cell_ref_echo": echo})
            carry = None  # the last cell ended on this page: its centre proves where it ends
            continue
        own = {id(a) for a in page_anchors if a.rest}  # date line with its own text
        carried: set[int] = set()
        attached: dict[int, list[Line]] = {id(a): [] for a in page_anchors}
        last_bottom: dict[int, float] = {id(a): a.bottom for a in page_anchors}
        above: dict[int, Line] = {}  # anchor -> amount line printed just above it (first narration line)
        to_next = _centred_narration(items) if mode == "below" else {}
        prev_anchor = None
        table_ended = False
        left = min(a.words[0].x0 for a in page_anchors)
        for ln in items:
            if ln.kind == "anchor":
                prev_anchor, carry, table_ended = ln, None, False
                continue
            if mode == "below" and ln.kind == "narr" and ln.words[0].x0 < left - 3:
                continue  # page furniture in the margin left of every row (HDFC footer 'HDFC BANK LIMITED')
            if mode == "below" and ln.kind == "narr" and _is_boilerplate(ln, money_x0):
                if prev_anchor is not None:  # end of the table on this page
                    carry = None  # (fine print over the first row is page header: HDFC 'Nomination : .. Account Type')
                prev_anchor = None
                table_ended = True
                continue
            if mode == "below" and prev_anchor is None and carry is not None and ln.kind == "narr":
                # narration of the previous page's last row continues at the top of this page
                raw, nx0 = carry
                if abs(ln.words[0].x0 - nx0) <= 5 and (money_x0 is None or ln.words[-1].x1 < money_x0):
                    raw.narration_parts[0] = (raw.narration_parts[0] + " " + ln.text).strip()
                    carried.add(id(ln))
                continue
            if mode == "below" and id(ln) in to_next:
                target = to_next[id(ln)]  # first line(s) of a description centred on the next date line
                attached[id(target)].append(ln)
                continue
            if mode == "below":
                target = prev_anchor
                if ln.kind == "amt" and (target is None or target.cands):
                    nxt = next((a for a in page_anchors if a.top > ln.top), None)
                    if nxt is not None and _top_amount_for(ln, nxt, prev_anchor, lh):
                        # amount cell top-aligned one line above a vertically centred date + balance (JasperReports)
                        nxt.cands = list(ln.cands) + nxt.cands
                        if ln.dir_hint and not nxt.dir_hint:
                            nxt.dir_hint = ln.dir_hint
                        above[id(nxt)] = ln
                        continue
                    if nxt is not None and _top_balance_for(ln, nxt, prev_anchor, lh):
                        nxt.cands = nxt.cands + list(ln.cands)
                        above[id(nxt)] = ln
                        continue
                if ln.kind == "amt" and target is not None and target.cands and _wrapped_amount_for(ln, target, nxt, lh):
                    target.cands = sorted(target.cands + list(ln.cands), key=lambda c: c.x0)
                    if ln.dir_hint and not target.dir_hint:
                        target.dir_hint = ln.dir_hint
                    attached[id(target)].append(ln)
                    last_bottom[id(target)] = max(last_bottom[id(target)], ln.bottom)
                    continue
                if ln.kind == "amt" and target is not None and target.cands:
                    # numbers but the previous row already has its numbers: try the next anchor without numbers
                    target = nxt if (nxt is not None and not nxt.cands and abs(nxt.mid - ln.mid) < 3 * lh) else None
                if target is None:
                    continue
                if ln.kind == "narr" and ln.top - last_bottom[id(target)] > 2.6 * lh:
                    continue
            else:
                pool = [a for a in page_anchors if not (ln.kind == "amt" and a.cands)]
                if not pool:
                    continue
                target = min(pool, key=lambda a: abs(a.mid - ln.mid))
                if abs(target.mid - ln.mid) > 3 * lh:
                    continue
            if ln.kind == "amt" and target.cands:
                continue
            attached[id(target)].append(ln)
            last_bottom[id(target)] = max(last_bottom[id(target)], ln.bottom)
            if ln.kind == "amt":
                target.cands = ln.cands
                if ln.dir_hint and not target.dir_hint:
                    target.dir_hint = ln.dir_hint
        wrap_lines = _wrap_lines([[a.rest] + [ln.rest if ln.kind == "amt" else ln.words
                                              for ln in sorted(attached[id(a)], key=lambda l: l.top) if id(ln) not in to_next]
                                  for a in page_anchors if a.cands])
        for a in page_anchors:
            extra = attached[id(a)]
            if not a.cands:
                diag["dangling"] = diag.get("dangling", 0) + 1  # a date line that ended up with no amounts (self-heal proof)
                continue
            parts = [" ".join(w.t for w in a.rest)]
            last_ws = a.rest  # words of the last line appended below (see _broken_token)
            tops = [a.top]
            if id(a) in above:
                up = above[id(a)]
                parts.insert(0, " ".join(w.t for w in up.rest))
                tops.insert(0, up.top)
            for ln in sorted(extra, key=lambda l: l.top):
                if id(ln) in to_next:  # printed above the date line
                    # without the serial no printed in the 'Sr No' column left of the date ('2.')
                    parts.insert(len(parts) - 1, " ".join(w.t for w in ln.words if w.x1 > a.words[0].x0))
                    continue
                ws_ = ln.rest if ln.kind == "amt" else ln.words
                if wrap_lines and parts[-1] and _broken_token(last_ws, ws_, wrap_lines):
                    parts[-1] += ws_[0].t
                    parts.append(" ".join(w.t for w in ws_[1:]))
                else:
                    parts.append(" ".join(w.t for w in ws_))
                last_ws = ws_
                if ln.kind == "amt":
                    tops.append(ln.top)
            narration = " ".join(p for p in parts if p).strip()
            raws.append(RawTxn(date=a.anchor_dates[0], value_date=a.anchor_dates[1] if len(a.anchor_dates) > 1 else None,
                               narration_parts=[narration], cands=list(a.cands), dir_hint=a.dir_hint,
                               page=a.page, tops=tops))
        if mode == "below":
            ok, ev = _alignment_proof(items, page_anchors, attached, carried, own, bool(to_next or above), money_x0)
        else:
            ok, ev = False, {"why": "centered layout"}
        aligned_pages += ok
        res.page_proof.append({"page": pno + 1, "proven": ok, **ev})
        last = page_anchors[-1]
        carry = None
        # fine print after the last row of a page that is not the last table page is the page footer, not the end of
        # the table: the row may still continue at the top of the next page (HDFC prints its disclaimer on every page)
        # (on the last table page too: HDFC wraps the final row onto the summary page; that page is read only up
        # to its summary heading / first number, see above)
        if mode == "below" and last.cands and last.rest and raws:
            carry = (raws[-1], last.rest[0].x0)
    # evenly spaced pages: geometry cannot place a wrap line, the bank's own reference numbers can. HDFC repeats a
    # row's Chq/Ref No ('0000321306450753') in the narration lines under its date line ('-321306450753-EASEBUZ'):
    # hundreds of such echoes on the row above and none on the row below prove the lines continue the row above.
    # Centred layouts echo the row below instead (Bank of Maharashtra: 889 of 889) and stay unproven
    # a page holding one row (and its wrap lines) has no row gap of its own: the same table's other pages give it
    spaced = [p["row_min"] for p in res.page_proof if p["why"] == "spacing" and p.get("row_min")]
    for p in res.page_proof:
        if p["why"] == "single row" and spaced and 1.4 * p["wrap_max"] <= min(spaced):
            p["proven"], p["why"] = True, "spacing of the other pages"
            aligned_pages += 1
    even = [p for p in res.page_proof if p["why"] == "even spacing"]
    e_own = sum(p.get("echo_own", 0) for p in res.page_proof)
    e_next = sum(p.get("echo_next", 0) for p in res.page_proof)
    if even and e_next == 0 and e_own >= 20:
        # each page needs its own evidence: an echo on that page, or no wrap line to place at all
        for p in even:
            if p.get("echo_own", 0) > 0 or not p.get("multiline"):
                p["proven"], p["why"] = True, f"reference echo ({e_own} rows in file, {p.get('echo_own', 0)} here, 0 on the next row)"
                aligned_pages += 1
            else:
                p["why"] = "even spacing, no reference echo on this page"
    _type_markers_after_balance(raws, layout, res.opening_balance)
    res.raw = raws
    res.table_pages = sum(1 for items in items_by_page if any(ln.kind == "anchor" for ln in items))
    res.grid_pages = grid_pages
    res.aligned_pages = aligned_pages
    if grid_pages:
        res.method += f"/grid{grid_pages}of{res.table_pages}"
    if cell_pages:
        res.method += f"/cells{cell_pages}of{res.table_pages}"
    if aligned_pages:
        res.method += f"/aligned{aligned_pages}of{res.table_pages}"
    if summary_blocks > 1:
        res.warnings.append(f"File contains {summary_blocks} statement summaries - looks like several statements "
                            "merged into one PDF; printed-summary check skipped.")
    elif summary:
        res.printed_summary = summary
    return res
