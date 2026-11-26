"""analyze_file(): file -> full JSON report (see REPORT_SCHEMA.md)."""
from __future__ import annotations

import os
import re
from datetime import datetime

import fitz

from . import analytics, forensics
from .banks import bank_certification, detect_bank
from .models import ParseResult
from . import selfheal, upi_contacts
from .pdf_parser import parse_pdf
from .reconcile import reconciliation, resolve
from .utils import find_dates, mask_account

ENGINE_VERSION = "0.1.0"


class PasswordRequired(Exception):
    """PDF is encrypted and the password is missing or wrong."""


class UnsupportedFile(Exception):
    """Not a statement we can read."""


class AAStatement(UnsupportedFile):
    """Account Aggregator statement (rendered by an FIU / AA portal from AA data). BSA analyses only PDFs
    downloaded from the bank itself, so these are refused - not judged fake, just out of scope."""


# Phrases printed on every AA-rendered statement seen so far (53/53 samples) and on no bank-generated PDF.
AA_MARKERS = ("account aggregator", "(fip)", "fiplive", "finsense")
AA_MESSAGE = ("This is an Account Aggregator (AA) statement. BSA analyses only statements downloaded from the "
              "bank's net-banking / app. Please upload the bank-downloaded PDF.")


def is_aa_statement(doc: fitz.Document) -> bool:
    pages = sorted({0, 1, doc.page_count - 1} & set(range(doc.page_count)))
    text = " ".join(doc[i].get_text() for i in pages).lower()
    return any(m in text for m in AA_MARKERS)


# Codecs that only make sense for images - page text never uses them. Online audit 2026-11-23: corpus_005, a 123 KB
# page whose content streams are JBIG2, kept MuPDF busy > 2 minutes inside get_text().
_IMAGE_CODECS = ("/JBIG2Decode", "/DCTDecode", "/JPXDecode", "/CCITTFaxDecode")
_INFLATE_CAP = 64 * 1024 * 1024          # one page content stream
_INFLATE_TOTAL = 512 * 1024 * 1024       # all of them together
_DAMAGED = "This PDF is damaged ({}) - download the statement again from the bank."


def check_pdf_sane(doc: fitz.Document) -> None:
    """Refuse (UnsupportedFile) files the reader must not touch: no pages, a broken page tree (online audit
    corpus_015 'cycle in page tree' crashed the analysis), page content in an image codec, or a decompression bomb.
    Runs before any text extraction; costs one inflate of each page content stream."""
    import zlib
    if doc.page_count == 0:
        raise UnsupportedFile(_DAMAGED.format("no readable pages"))
    streams: list[tuple[int, str]] = []          # (xref, where) - what text extraction will decode
    for i in range(doc.page_count):
        try:
            streams += [(x, f"page {i + 1}") for x in doc.load_page(i).get_contents()]
        except Exception as e:
            raise UnsupportedFile(_DAMAGED.format(f"page {i + 1} can't be read")) from e
    seen = {x for x, _ in streams}
    for x in range(1, doc.xref_length()):        # Form XObjects are decoded too (skeptic 2026-11-23, R2)
        try:
            if x not in seen and doc.xref_is_stream(x) and doc.xref_get_key(x, "Subtype")[1] == "/Form":
                streams.append((x, f"form object {x}"))
        except Exception:
            continue
    total = 0
    for x, where in streams:
        try:
            filt = doc.xref_get_key(x, "Filter")[1] or ""
        except Exception:
            continue
        names = re.findall(r"/[A-Za-z0-9]+", filt)  # '/FlateDecode' or '[/FlateDecode]' (skeptic R3)
        if any(c in names for c in _IMAGE_CODECS):
            raise UnsupportedFile(_DAMAGED.format(f"{where} text is stored as an image stream ({filt})"))
        if names != ["/FlateDecode"]:
            continue  # other / chained filters: the hard time limit around the analysis covers them
        try:
            z = zlib.decompressobj()
            n = len(z.decompress(doc.xref_stream_raw(x) or b"", _INFLATE_CAP))
        except Exception:
            continue  # unusual / encrypted raw bytes: MuPDF copes or fails later as usual
        total += n
        if z.unconsumed_tail or total > _INFLATE_TOTAL:
            raise UnsupportedFile(_DAMAGED.format(f"{where} inflates to over "
                                                  f"{(_INFLATE_CAP if z.unconsumed_tail else total) >> 20} MB"))


_TITLE = r"(?:MR|MRS|MS|MISS|M/S|SHRI|SMT|DR)\.?"
NAME_RES = [
    re.compile(r"(?:account\s*(?:holder\s*)?names?(?![a-z])|customer\s*name|a/c\s*name|name\s*of\s*(?:the\s*)?customer|"
               r"account\s*title|customer\s*details)[ \t]*[:\-]?[ \t]*([A-Za-z][A-Za-z .'&]{2,60})", re.I),
    re.compile(r"^[ \t]*name[ \t]*[:\-][ \t]*([A-Za-z][A-Za-z .'&]{2,60})", re.I | re.M),
    # 'MR SUMIT SINGH' alone, or followed by the right-hand column's 'Label :' (HDFC 'MR. X City : CHENNAI')
    re.compile(r"^[ \t]*(" + _TITLE + r"[ \t]+[A-Z][A-Za-z .'&]{2,50}?)(?=[ \t]*$|[ \t]+[A-Za-z][A-Za-z.]*[ \t]*:)",
               re.M),
    # unlabelled name on the line under the product title (HSBC: 'SAVINGS ACCOUNT - RES' / 'PRADEEP GUPTA')
    re.compile(r"^(?:SAVINGS|CURRENT|SALARY)\s+(?:ACCOUNT|A/C)\b[^\n]*\n\s*([A-Z][A-Z.'&]*(?: [A-Z][A-Z.'&]*){1,4})"
               r"(?= {2,}| Available\b|\s*$)", re.M),
    # Finacle net-banking export: 'Transactions List - KAPIL R VYAS - 0581..' / 'Account Number 0581..(INR) - NAME'
    re.compile(r"^Transactions\s+List\s*-[ \t]*-?[ \t]*([A-Za-z][A-Za-z .'&]{2,60}?)[ \t]*(?:\(INR\)[ \t]*)?-[ \t]*\d",
               re.M | re.I),
    re.compile(r"^Account\s+Number[ \t]+\d{6,18}[ \t]*\(INR\)[ \t]*-[ \t]*([A-Za-z][A-Za-z .'&]{2,60})$", re.M | re.I),
    # Kotak: 'Akshay Shivajirao Lakhkar Account No. 59469..' (name left, account number right)
    re.compile(r"^([A-Z][A-Za-z.']+(?:[ \t]+[A-Z][A-Za-z.']+){1,4})[ \t]+Account[ \t]+No\.?[ \t]*[:\-]?[ \t]*\d", re.M),
    # unlabelled name directly above the relation line (SBM: 'POONAM R SHINDE User ...' / 'D/O: R SHINDE ...')
    re.compile(r"^\s*([A-Z][A-Z.']*(?: [A-Z][A-Z.']*){1,4})(?=\s|$)[^\n]*\n\s*[SDWC]/O\b", re.M),
    # ICICI 'Statement of Transactions': 'VIJAY KUMAR SEHGAL Your Base Branch: ICICI BANK LIMITED,'
    re.compile(r"^[ \t]*([A-Z][A-Z.']*(?: [A-Z][A-Z.']*){1,4})[ \t]+Your[ \t]+Base[ \t]+Branch\b", re.M),
    # Union Bank (JasperReports): 'Name & Address :' with the name at the start of the next line; the right-hand
    # column continues on both lines ('Name & Address : Customer ID : ..' / 'MORTHA SURI BABU Account Number : ..')
    re.compile(r"(?i:name)\s*(?:&|(?i:and))\s*(?i:address)[ \t]*[:\-]?[^\n]*\n[ \t]*([A-Z][A-Z.'&]*(?: [A-Z][A-Z.'&]*){1,4})"
               r"(?=[ \t]+[A-Z][a-z]|[ \t]*$)", re.M),
    # two-line label with the value centred between its halves: 'Customer' / 'PATIL RAVINDRA ...' / 'Name :'
    re.compile(r"^[ \t]*(?i:customer)\b[^\n]*\n[ \t]*([A-Z][A-Z.'&]*(?: [A-Z][A-Z.'&]*){1,6})[^\n]*\n[ \t]*(?i:name)[ \t]*[:\-]",
               re.M),
]
# bank-specific unlabelled holder lines, tried only on that bank's statements (bank_code from detect_bank).
# Airtel Payments Bank: the name alone, in title case, between the 'Account Statement' title and 'Account Number:'
BANK_NAME_RES = {
    "AIRTEL": re.compile(r"^[ \t]*Account[ \t]+Statement[ \t]*\n[ \t]*([A-Za-z][A-Za-z.']*(?:[ \t]+[A-Za-z][A-Za-z.']*)"
                         r"{0,5})[ \t]*\n[ \t]*Account[ \t]+Number[ \t]*:", re.M),
    # Canara: 'Name <HOLDER> Branch Name <BRANCH>' - label without a colon, the branch column on the same line
    "CANARA": re.compile(r"^[ \t]*Name[ \t]+([A-Z][A-Z.']*(?:[ \t]+[A-Z][A-Z.']*){0,5}?)[ \t]+Branch[ \t]+Name\b", re.M),
    # Karur Vysya: the holder alone (bold) on the line under the 'ACCOUNT STATEMENT' title, left of 'Acc.No. :'
    "KVB": re.compile(r"^[ \t]*ACCOUNT[ \t]+STATEMENT[ \t]*\n[ \t]*([A-Z][A-Z.']*(?:[ \t]+[A-Z][A-Z.']*){0,5}?)"
                      r"[ \t]+Acc\.[ \t]*No\.?[ \t]*:", re.M),
}
# bank-specific account number labels the generic ACC_RE misses, tried only on that bank's statements
BANK_ACC_RES = {
    # Canara: 'Statement for A/c XXXXXXXX8520 for the period ..' (printed masked)
    "CANARA": re.compile(r"^[ \t]*Statement[ \t]+for[ \t]+A/c[ \t]+([0-9Xx*]{6,20})[ \t]+for[ \t]+the[ \t]+period\b",
                         re.M | re.I),
    # Karur Vysya: 'Acc.No. : 1851166000041130'
    "KVB": re.compile(r"\bAcc\.[ \t]*No\.?[ \t]*:[ \t]*(\d{9,18})\b"),
    # Saraswat: 'Account No. : SBSTF/035200100100308' (scheme code before the number)
    "SARASWAT": re.compile(r"^[ \t]*Account[ \t]+No\.?[ \t]*:[ \t]*[A-Z]{2,8}/(\d{9,18})\b", re.M),
}


def _bank_header_fields(info: dict, bank_code: str, text: str) -> None:
    """Holder / account number from the bank-scoped patterns above (they win over the generic ones)."""
    m = BANK_NAME_RES[bank_code].search(text) if bank_code in BANK_NAME_RES else None
    if m and not NAME_JUNK.search(m.group(1)):
        info["account_holder"] = re.sub(r"\s+", " ", m.group(1)).strip(" .").upper()
    m = BANK_ACC_RES[bank_code].search(text) if bank_code in BANK_ACC_RES else None
    if m:
        info["account_number"] = mask_account(m.group(1))
NAME_STOP = re.compile(r"\s+(account|a/c|branch|address|period|cust|customer|crn|mobile|email|ifsc|statement|"
                       r"date|joint|nominee|phone|pan|currency|type|status|saving|savings|nickname|primary|gstin)\b.*$", re.I)
# a captured value that is really a label or a product word, not a person
NAME_JUNK = re.compile(r"^(?:address|name|details|nil|na|n/a|savings|current|saving|individual|self|primary)\b|"
                       r"bank|statement|branch", re.I)
ACC_RE = re.compile(r"(?:a/?c(?:count)?\.?\s*(?:no|number|#)\.?|account\s*(?:no|number|#)\.?|account\s*#|"
                    r"account\s*:)\s*[:\-]?\s*(?:[A-Z]{2,3}-)?([0-9Xx*](?:-?[0-9Xx*]){5,19})", re.I)  # '133-231993-006' too
ACC_FINACLE_RE = re.compile(r"^Transactions\s+List\s*-.*?-[ \t]*(\d{9,18})\b", re.M | re.I)
# label 'Account' lost from the header cell, only 'Number: 7852...' left at the start of a line (Karnataka Bank)
ACC_BARE_RE = re.compile(r"^\s*number\s*:\s*(\d{9,18})\b", re.I | re.M)
OWN_IFSC = re.compile(r"IFS\s*C?\s*(?:Code)?\s*[:\-]?\s*([A-Z]{4}0[A-Z0-9]{6})", re.I)
# the bank's own product label calls the account a salary account: Yes 'Account Variant/ Description: SA - SMART
# SALARY EXCLUSIVE', IDFC 'ACCOUNT TYPE : Corporate Salary Platinum' (matched on the upper-cased, one-line header)
SALARY_ACCOUNT_RE = re.compile(r"\b(?:ACCOUNT\s*(?:TYPE|VARIANT)|PRODUCT\s*(?:NAME|DESCRIPTION)|SCHEME\s*NAME)\b"
                               r"[\s/:\-A-Z]{0,40}?\bSALARY\b")
# unlabelled holder line directly above the address (SIB: 'SYED ABDUL KHADER' / '33/14,VENKATASAMI 2ND STREET')
UNLABELLED_NAME_RE = re.compile(r"^((?:" + _TITLE + r" )?[A-Z]{2,}(?: [A-Z][A-Z.']*){1,4})\n(?=\d+[/,\-])", re.M)
ADDRESS_WORDS = {"ROAD", "STREET", "NAGAR", "COLONY", "COMPLEX", "INDIA", "BANK", "BRANCH", "LANE", "MARG", "CROSS",
                 "MAIN", "FLOOR", "TOWER", "BUILDING", "SECTOR", "PHASE", "NEAR", "OPP", "DIST", "TALUK", "STATE",
                 "NADU", "PRADESH", "STATEMENT", "ACCOUNT", "CUSTOMER", "DETAILS", "LIMITED", "LTD", "PVT"}


def _clean_name(text: str, m: re.Match) -> str | None:
    nm = m.group(1)
    # value ran into the next column's label: 'AISHWARY NIRAJ SINHA Status: Active'
    if re.match(r"[ \t]*:", text[m.end(1):m.end(1) + 4]) and " " in nm.strip():
        nm = nm.strip().rsplit(" ", 1)[0]
    nm = NAME_STOP.sub("", nm)
    toks = nm.split()
    # an upper-case name followed by mixed-case label words ('PALLALA ABHILASH City')
    if len(toks) >= 2 and toks[0].isupper() and toks[1].isupper():
        for i, t in enumerate(toks):
            if any(ch.islower() for ch in t):
                toks = toks[:i]
                break
    nm = " ".join(toks).strip(" .:-")
    if len(nm) < 3 or NAME_JUNK.search(nm):
        return None
    return re.sub(r"\s+", " ", nm).upper()


_LABEL_LEAD = {"customer", "account", "holder", "a/c", "holder's", "accountholder"}
_NAME_TOKEN = re.compile(r"[A-Za-z][A-Za-z.'&]*")


def _upright_words(page) -> list[tuple]:
    """Words as (x0, y0, x1, y1, text) in reading orientation (rotated pages are turned upright)."""
    mat = page.rotation_matrix
    out = []
    for w in page.get_text("words"):
        r = fitz.Rect(w[:4]) * mat
        out.append((r.x0, r.y0, r.x1, r.y1, w[4]))
    return sorted(out, key=lambda w: (round(w[1]), w[0]))


def _holder_from_layout(doc) -> str | None:
    """Positional 'Customer Name' / 'Account Holder Name' lookup on page 1: the value sits to the right of the
    label, inside the label's vertical band (a label printed on two lines has the value centred between them),
    and may wrap onto the next line of the same column (IDFC: 'Mr. Akshay Shivajirao L' / 'akhkar')."""
    if doc is None or not doc.page_count:
        return None
    ws = _upright_words(doc[0])

    def yc(w):
        return (w[1] + w[3]) / 2

    for i, w in enumerate(ws):
        if not re.fullmatch(r"name\s*:?", w[4], re.I):
            continue
        lead = [p for p in ws if p[4].lower().rstrip(":") in _LABEL_LEAD and (
            (abs(yc(p) - yc(w)) < 3 and 0 <= w[0] - p[2] < 20) or            # 'Customer Name'
            (abs(p[0] - w[0]) < 6 and 0 <= w[1] - p[3] < 8))]                  # 'Customer' above 'Name'
        if not lead:
            continue
        label = lead + [w]
        edge = w[2]
        colon = [p for p in ws if p[4] == ":" and abs(yc(p) - yc(w)) < 3 and 0 <= p[0] - w[2] < 15]
        if colon:
            edge = colon[0][2]
        y0, y1 = min(p[1] for p in label) - 2, max(p[3] for p in label) + 2
        row = sorted((p for p in ws if p[0] >= edge - 1 and p[0] < edge + 320 and y0 <= yc(p) <= y1),
                     key=lambda p: p[0])
        while row and row[0][4] in (":", "-"):
            row = row[1:]
        val, gap_after = [], False
        for k, p in enumerate(row):
            if val and p[0] - val[-1][2] > 18:
                gap_after = True
                break
            if p[4].endswith(":") or (k + 1 < len(row) and row[k + 1][4] == ":"):
                gap_after = True
                break
            val.append(p)
        if not val or not all(_NAME_TOKEN.fullmatch(p[4]) for p in val):
            continue
        gap_after = gap_after or any(p[0] > val[-1][2] + 18 and y0 <= yc(p) <= y1 for p in ws)
        toks = [p[4] for p in val]
        # wrapped onto the next line of the same column, only when a second column cut it short
        h = val[0][3] - val[0][1]
        nxt = [p for p in ws if val[0][3] - 1 < p[1] < val[0][3] + 1.2 * h]
        if gap_after and nxt and not any(p[2] <= edge for p in nxt):
            first = min(nxt, key=lambda p: p[0])
            rest = sorted((p for p in nxt if p is not first and p[0] > first[0]), key=lambda p: p[0])
            if abs(first[0] - val[0][0]) < 4 and re.fullmatch(r"[A-Za-z]+", first[4]):
                if first[4][0].islower():
                    toks[-1] += first[4]
                elif rest and re.fullmatch(r"[A-Z]{2,}", rest[0][4]) and any(p[4].endswith(":") or p[4] == ":"
                                                                             for p in rest[:3]):
                    toks.append(first[4])
        nm = " ".join(toks).strip(" .:-")
        if 3 <= len(nm) <= 60 and len(toks) <= 6 and not NAME_JUNK.search(nm) and not NAME_STOP.search(" " + nm):
            return nm.upper()
    return None


def _holder_above_address(doc) -> str | None:
    """Unlabelled holder printed directly above the address block, left-aligned with it (Jana: 'SANKAR D' over
    'Address :'; SBM: 'POONAM R SHINDE' over 'D/O: ...'). Last resort, page 1 only."""
    if doc is None or not doc.page_count:
        return None
    ws = _upright_words(doc[0])
    for a in ws:
        if not re.fullmatch(r"address\s*:?|[SDWC]/O:?", a[4], re.I):
            continue
        h = a[3] - a[1]
        above = [p for p in ws if abs(p[0] - a[0]) < 6 and a[1] - 2.5 * h < p[1] < a[1] - 0.5 * h]
        if not above:
            continue
        start = max(above, key=lambda p: p[1])
        row = sorted((p for p in ws if abs((p[1] + p[3]) / 2 - (start[1] + start[3]) / 2) < 3 and p[0] >= start[0]),
                     key=lambda p: p[0])
        toks = []
        for p in row:
            if toks and p[0] - toks[-1][2] > 18:
                break
            toks.append(p)
        words = [p[4] for p in toks]
        if not (1 < len(words) <= 5 and all(re.fullmatch(r"[A-Z][A-Z.']*", w) for w in words)):
            continue
        if set(words) & ADDRESS_WORDS or NAME_JUNK.search(" ".join(words)):
            continue
        return " ".join(words)
    return None


def _header_info(text: str, doc=None) -> dict:
    info = {"account_holder": None, "account_number": None, "ifsc": None, "period_from": None, "period_to": None,
            "salary_account": bool(SALARY_ACCOUNT_RE.search(re.sub(r"\s+", " ", (text or "").upper())))}
    for rx in NAME_RES:
        m = rx.search(text)
        if m:
            nm = _clean_name(text, m)
            if nm:
                info["account_holder"] = nm
                break
    lay = _holder_from_layout(doc) if doc is not None else None
    held = info["account_holder"]
    if lay and (not held or (lay.startswith(held) and len(lay) > len(held))):  # text copy cut by a column wrap
        info["account_holder"] = lay
    if not info["account_holder"]:
        for m in UNLABELLED_NAME_RE.finditer("\n".join(text.splitlines()[:30])):
            if not set(re.findall(r"[A-Z]+", m.group(1))) & ADDRESS_WORDS:
                info["account_holder"] = m.group(1).strip()
                break
    if not info["account_holder"] and doc is not None:
        info["account_holder"] = _holder_above_address(doc)
    m = ACC_RE.search(text) or ACC_FINACLE_RE.search(text) or ACC_BARE_RE.search(text)
    if m:
        info["account_number"] = mask_account(m.group(1).replace("-", ""))
    m = OWN_IFSC.search(text)
    if m:
        info["ifsc"] = m.group(1).upper()
    for line in text.splitlines():
        low = line.lower()
        if ("from" in low or "period" in low or " to " in low or "statement" in low) or re.search(r"\d\s*-\s*\d", line):
            ds = find_dates(line)
            if len(ds) >= 2 and ds[0][2] < ds[1][2]:
                info["period_from"], info["period_to"] = ds[0][2], ds[1][2]
                break
    return info


def _name_match(applicant: str | None, holder: str | None) -> dict:
    if not applicant:
        return {"applicant": None, "holder": holder, "status": "NOT_CHECKED", "score": None}
    a = {w for w in re.findall(r"[A-Z]{2,}", applicant.upper()) if w not in ("MR", "MRS", "MS", "MISS", "SHRI", "SMT")}
    h = {w for w in re.findall(r"[A-Z]{2,}", (holder or "").upper()) if w not in ("MR", "MRS", "MS", "MISS", "SHRI", "SMT")}
    if not h:
        return {"applicant": applicant, "holder": holder, "status": "NOT_CHECKED", "score": None}
    # allow initials: "K S REDDY" vs "KEESARI SURENDAR REDDY"
    hit = len(a & h)
    init = sum(1 for w in a - h if any(x.startswith(w) or w.startswith(x) for x in h - a))
    score = round(100 * (hit + 0.5 * init) / max(len(a), 1))
    status = "MATCH" if score >= 75 else ("PARTIAL" if score >= 40 else "MISMATCH")
    return {"applicant": applicant, "holder": holder, "status": status, "score": score}


# AU internet-banking export stops at 999 rows (AU_009: rows end 14-Aug, header period to 15-Oct): a row count
# on a round cap with the last row well before the printed period end means the rest was never exported
EXPORT_ROW_CAPS = (500, 999, 1000, 2000, 5000)


def _export_truncation(n_rows: int, last_txn, period_to) -> dict | None:
    if not period_to or n_rows not in EXPORT_ROW_CAPS or (period_to - last_txn).days <= 3:
        return None
    return {"rows": n_rows, "rows_end": last_txn.isoformat(), "header_period_to": period_to.isoformat()}


def data_quality(recon: dict, verify_only: bool = False, rows_proven: bool = True) -> dict:
    """Can these numbers drive an underwriting decision? Only a fully reconciled balance chain proves every row.
    READY      every row reconciles and the closing balance matches - figures are exact.
    REVIEW     >= 98% of rows reconcile - figures usable only after a human checks the listed rows.
    NOT_USABLE fewer rows reconcile, or transactions could not be read - do not use the analytics.
    rows_proven: the row boundaries came from the file itself (drawn row borders, or line spacing / the bank's own
    reference numbers proving which row each wrap line continues - pdf_parser._alignment_proof - or spreadsheet
    cells). Without that, amounts can reconcile while a narration sits on the neighbouring row - so at best REVIEW."""
    if verify_only or recon["status"] == "UNVERIFIED" or not recon["rows_total"]:
        return {"level": "NOT_USABLE", "underwriting_ready": False, "unverified_rows": recon["rows_total"],
                "unverified_amount": None,
                "message": "Transactions could not be read and proven from this file. Do not use for underwriting."}
    bad = recon["rows_total"] - recon["rows_verified"]
    amt = round(sum(abs(m.get("amount") or 0) for m in recon.get("mismatch_rows") or []), 2)
    if recon["status"] == "MATCHED" and not recon["rows_mismatch"]:  # first row of an inferred opening is unprovable, not wrong
        if rows_proven:
            return {"level": "READY", "underwriting_ready": True, "unverified_rows": 0, "unverified_amount": 0.0,
                    "rows_proven": True,
                    "message": "Every row reconciles with the running balance and the closing balance matches."}
        return {"level": "REVIEW", "underwriting_ready": False, "unverified_rows": 0, "unverified_amount": 0.0,
                "rows_proven": False,
                "message": "Every amount reconciles, but this layout does not prove which narration belongs to which "
                           "row. Check the salary / EMI / bounce rows against the PDF."}
    if not recon["rows_mismatch"] and recon["closing_match"] and recon.get("printed_summary_match") is False:
        # every row chains and the closing matches, but the bank's printed summary disagrees with the rows
        return {"level": "REVIEW", "underwriting_ready": False, "unverified_rows": bad, "unverified_amount": 0.0,
                "message": "Every row reconciles with the running balance, but the statement's printed summary "
                           "(totals / closing balance) does not match the rows. Check the summary against the PDF "
                           "before using income/EMI figures."}
    if recon["verified_pct"] >= 98.0:
        return {"level": "REVIEW", "underwriting_ready": False, "unverified_rows": bad, "unverified_amount": amt,
                "message": f"{bad} row(s) worth Rs {amt:,.2f} do not reconcile"
                           + ("" if recon["closing_match"] else " and the closing balance does not match")
                           + ". Check those rows before using income/EMI figures."}
    return {"level": "NOT_USABLE", "underwriting_ready": False, "unverified_rows": bad, "unverified_amount": amt,
            "message": f"Only {recon['verified_pct']}% of rows reconcile with the running balance - the parsed figures "
                       "cannot be trusted. Do not use for underwriting; review the PDF manually."}


def _quality_flag(dq: dict) -> list[dict]:
    if dq["level"] == "NOT_USABLE":
        return [{"severity": "high", "code": "DATA_NOT_VERIFIED", "message": dq["message"]}]
    if dq["level"] == "REVIEW":
        return [{"severity": "medium", "code": "DATA_REVIEW", "message": dq["message"]}]
    return []


def _verify_only_report(doc, raw_bytes, pr: ParseResult, fname: str, was_encrypted: bool, warnings: list,
                        password: str | None = None) -> dict:
    recon = {"status": "UNVERIFIED", "opening_balance": None, "closing_balance": None, "total_credits": 0.0,
             "total_debits": 0.0, "computed_closing": None, "closing_match": False, "rows_total": 0,
             "rows_verified": 0, "rows_mismatch": 0, "verified_pct": 0.0, "mismatch_rows": [],
             "printed_summary": None, "printed_summary_match": None,
             "name_match": {"applicant": None, "holder": None, "status": "NOT_CHECKED", "score": None}}
    bank_code, bank_name, bank_method = detect_bank(pr.header_text, pr.page1_text, doc=doc)
    # no bank and no rows = not recognisably a bank statement (online audit 2026-11-23: Word / react-pdf docs got
    # ORIGINAL 90); bank_code also drives the bank-gated generator / blank-metadata checks
    auth = forensics.analyze_pdf(doc, raw_bytes, [], recon, None, was_encrypted, 0.0, header_text=pr.header_text,
                                 password=password, bank_code=bank_code, recognised=bank_code != "UNKNOWN")
    info = _header_info(pr.header_text or pr.page1_text, doc)
    _bank_header_fields(info, bank_code, pr.header_text or pr.page1_text or "")
    if pr.text_chars < 50 * max(1, pr.pages):
        warnings.append("No text layer - scanned/photographed/screenshot PDF. Only bank-generated PDFs are accepted.")
    else:
        warnings.append("Transactions could not be read from this layout; only the authenticity checks ran.")
    flags = [{"severity": "high", "code": "AUTHENTICITY",
              "message": f"Document authenticity: {auth['verdict'].lower()} (score {auth['score']})."}] \
        if auth["verdict"] != "ORIGINAL" else []
    empty_sum = {"monthly_income": None, "income_source": "none", "avg_monthly_credits": 0.0,
                 "avg_monthly_debits": 0.0, "abb": None, "min_eod_balance": None, "max_eod_balance": None,
                 "existing_emi": 0.0, "short_term_repayment": 0.0, "bounce_count": 0, "distinct_lenders": 0,
                 "salary_detected": False, "low_balance_days_pct": None,
                 "cash_deposit_pct": 0.0}
    meta = {"file_name": fname, "file_type": "pdf", "pages": pr.pages, "bank_code": bank_code, "bank_name": bank_name,
            "bank_method": bank_method, "bank_certification": bank_certification(bank_code),
            "account_holder": info["account_holder"],
            "account_number": info["account_number"], "ifsc": info["ifsc"],
            "period_from": info["period_from"].isoformat() if info["period_from"] else None,
            "period_to": info["period_to"].isoformat() if info["period_to"] else None, "months_covered": 0,
            "txn_count": 0, "parse_method": "verify-only", "parse_confidence": 0.0, "engine_version": ENGINE_VERSION,
            "analyzed_at": datetime.now().isoformat(timespec="seconds"), "verify_only": True}
    dq = data_quality(recon, verify_only=True)
    return {"meta": meta, "authenticity": auth, "reconciliation": recon, "summary": empty_sum,
            "salary": {"detected": False, "confidence": 0, "employer": None, "avg_amount": 0.0, "usual_day": None,
                       "months": [], "short_history": False, "candidates": []},
            "loans": {"emis": [], "monthly_obligation": 0.0, "short_term_repayments": {"monthly": 0.0, "lenders": 0},
                      "disbursals": [], "oneoff_repayments": [],
                      "credit_card": {"count": 0, "total": 0.0}, "distinct_lenders": []},
            "bounces": {"count": 0, "charges_count": 0, "events": [], "months_with_bounce": []},
            "top_credits": [], "top_debits": [], "monthly": [], "categories": {"credits": [], "debits": []},
            "counterparties": {"top_payers": [], "top_payees": []},
            "cash": {"deposits_count": 0, "deposits_amount": 0.0, "withdrawals_count": 0, "withdrawals_amount": 0.0},
            "risk_flags": _quality_flag(dq) + flags, "transactions": [], "warnings": warnings, "data_quality": dq,
            "upi_mobiles": upi_contacts.empty()}


def analyze_file(path: str, password: str | None = None, applicant_name: str | None = None,
                 original_filename: str | None = None) -> dict:
    holder: dict = {}
    try:
        return _analyze(path, password, applicant_name, original_filename, holder)
    finally:
        doc = holder.get("doc")
        if doc is not None and not doc.is_closed:
            doc.close()  # Windows keeps a lock on open files; always release it


def _analyze(path, password, applicant_name, original_filename, holder) -> dict:
    fname = original_filename or os.path.basename(path)
    ext = os.path.splitext(fname)[1].lower() or os.path.splitext(path)[1].lower()
    warnings: list[str] = []
    healed = None
    doc = None
    raw_bytes = b""
    was_encrypted = False

    if ext == ".pdf":
        try:
            doc = fitz.open(path)
        except Exception as e:
            raise UnsupportedFile(f"Could not open PDF: {e}")
        holder["doc"] = doc
        if doc.needs_pass:
            was_encrypted = True
            if not password or not doc.authenticate(password):
                raise PasswordRequired("This PDF is password protected." if not password else "Wrong PDF password.")
        check_pdf_sane(doc)
        try:
            aa = is_aa_statement(doc)
        except Exception as e:  # MuPDF could not extract text at all
            raise UnsupportedFile(_DAMAGED.format(f"text can't be read: {type(e).__name__}")) from e
        if aa:
            raise AAStatement(AA_MESSAGE)
        with open(path, "rb") as f:
            raw_bytes = f.read()
        # normal parse first; the self-heal portfolio runs only when it lacks strong proof (selfheal.py)
        try:
            healed = selfheal.run(path, doc)
            pr: ParseResult = healed.ev.pr
        except Exception:  # the layer is a fallback: never let it fail an analysis
            healed = None
            pr = parse_pdf(path, doc=doc)
        file_type = "pdf"
    else:
        raise UnsupportedFile("Upload the bank's PDF statement.")

    warnings.extend(pr.warnings)
    if not pr.raw:
        if doc is not None:
            # still give a fake/original verdict - scanned or unreadable PDFs are exactly what fraudsters send
            return _verify_only_report(doc, raw_bytes, pr, fname, was_encrypted, warnings, password)
        raise UnsupportedFile("No transactions could be read from this file. " + " ".join(pr.warnings))

    if healed is not None:   # the healer already resolved the rows (default or the adopted strategy)
        txns, opening, contradictions = healed.ev.txns or [], healed.ev.opening, healed.ev.contradictions
        warnings.extend(healed.ev.warnings)
        if healed.info.get("winner") not in (None, "default"):
            warnings.append(f"Self-heal: layout read with strategy '{healed.info['winner']}' "
                            "(proved by the statement itself: chain, closing, dates, summary).")
    else:
        txns, opening, contradictions = resolve(pr.raw, pr.opening_balance, warnings)
    if not txns:
        raise UnsupportedFile("No transactions could be read from this file.")
    bank_code, bank_name, bank_method = detect_bank(pr.header_text, pr.page1_text,
                                                    "\n".join(t.narration for t in txns), doc)
    recon = reconciliation(txns, opening, pr.printed_summary, placeholder_opening=bank_code == "FINO")
    info = _header_info(pr.header_text or pr.page1_text, doc)
    _bank_header_fields(info, bank_code, pr.header_text or pr.page1_text or "")
    if not info["ifsc"]:
        m = OWN_IFSC.search(pr.page1_text or "")
        info["ifsc"] = m.group(1).upper() if m else None
    recon["name_match"] = _name_match(applicant_name, info["account_holder"])

    last_txn = max(t.date for t in txns)
    cut = (_export_truncation(len(txns), last_txn, info["period_to"])
           or _export_truncation(len(pr.raw), last_txn, info["period_to"]))
    # truncated export: coverage, EOD/ABB and monthly averages run to the last row, not the printed period end
    an = analytics.run(txns, info["account_holder"], info["period_from"], last_txn if cut else info["period_to"],
                       salary_account=info["salary_account"])
    if cut:
        msg = (f"Statement truncated by bank export limit: rows end {last_txn:%d-%b-%Y}, header says "
               f"{info['period_to']:%d-%b-%Y} ({cut['rows']} rows). Coverage and averages use the rows only - "
               f"get the rest of the period from the bank.")
        warnings.append(msg)
        an["risk_flags"].insert(0, {"severity": "high", "code": "STATEMENT_TRUNCATED", "message": msg})
    start, end = an["period"]

    if doc is not None:
        auth = forensics.analyze_pdf(doc, raw_bytes, txns, recon, txns[-1].date, was_encrypted, recon["verified_pct"],
                                     header_text=pr.header_text, password=password, bank_code=bank_code)
    else:
        auth = {"verdict": "CANNOT_VERIFY", "score": 50,
                "checks": [{"name": "File type", "status": "warn",
                            "detail": "Spreadsheet/CSV exports can be edited freely - authenticity cannot be proven "
                                      "from the file. Prefer the bank-downloaded PDF."}],
                "pdf_metadata": {"producer": None, "creator": None, "created": None, "modified": None,
                                 "encrypted": False, "signed": False, "eof_markers": 0}}
        forensics._content_checks(lambda n, s, d, p=0: auth["checks"].append({"name": n, "status": s, "detail": d}),
                                  recon, recon["verified_pct"])
    if recon["name_match"]["status"] == "MISMATCH":
        an["risk_flags"].insert(0, {"severity": "high", "code": "NAME_MISMATCH",
                                    "message": f"Account holder '{info['account_holder']}' does not match applicant "
                                               f"'{applicant_name}'."})
    if auth["verdict"] in ("FAKE", "SUSPICIOUS"):
        why = "; ".join(auth.get("fake_reasons") or [])
        an["risk_flags"].insert(0, {"severity": "high" if auth["verdict"] == "FAKE" else "medium",
                                    "code": "AUTHENTICITY",
                                    "message": f"Document is {auth['verdict'].lower()}" + (f": {why}" if why else "")
                                               + f" (score {auth['score']})."})
    if contradictions > 0.2 * len(txns):
        warnings.append("Column positions disagreed with the balance chain on many rows; directions taken from "
                        "the balance chain.")

    parse_conf = recon["verified_pct"] if recon["status"] != "UNVERIFIED" else 50.0
    # every table page must prove which line belongs to which row: drawn row borders, or _alignment_proof. All pages,
    # not 90%: the odd unproven page is where narrations go wrong (IDFC centred description dropped on p98)
    proven = file_type != "pdf" or (pr.table_pages > 0 and pr.grid_pages + pr.aligned_pages >= pr.table_pages)
    dq = data_quality(recon, rows_proven=proven)
    meta = {
        "file_name": fname, "file_type": file_type, "pages": pr.pages, "bank_code": bank_code,
        "bank_name": bank_name, "bank_method": bank_method, "bank_certification": bank_certification(bank_code),
        "account_holder": info["account_holder"],
        "account_number": info["account_number"], "ifsc": info["ifsc"],
        "period_from": (info["period_from"] or start).isoformat(), "period_to": (info["period_to"] or end).isoformat(),
        "months_covered": an["months_covered"], "truncated": cut, "txn_count": len(txns), "parse_method": pr.method,
        "parse_confidence": parse_conf, "engine_version": ENGINE_VERSION,
        "analyzed_at": datetime.now().isoformat(timespec="seconds"),
    }
    report = {
        "meta": meta, "authenticity": auth, "reconciliation": recon, "summary": an["summary"],
        "salary": an["salary"], "loans": an["loans"], "bounces": an["bounces"],
        "top_credits": an["top_credits"], "top_debits": an["top_debits"], "monthly": an["monthly"],
        "categories": an["categories"], "counterparties": an["counterparties"], "cash": an["cash"],
        "risk_flags": _quality_flag(dq) + an["risk_flags"], "data_quality": dq,
        "transactions": [{"idx": t.idx, "date": t.date.isoformat(),
                          "value_date": t.value_date.isoformat() if t.value_date else None,
                          "narration": t.narration, "debit": t.debit, "credit": t.credit, "balance": t.balance,
                          "category": t.category, "counterparty": t.counterparty, "verified": bool(t.verified),
                          "tags": sorted(set(t.tags))} for t in txns],
        "warnings": warnings,
        "selfheal": healed.info if healed is not None else None,
    }
    # UPI payments to / from mobile-number UPI IDs, grouped per phone (Excel sheet 'UPI Mobiles')
    report["upi_mobiles"] = upi_contacts.build(report["transactions"], bank_code, info["account_number"])
    return report
