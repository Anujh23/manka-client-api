"""
Payslip (salary slip) authenticity check.

Local PDF forensics (software, edit dates, saves, overlays, fonts) + a Gemini verdict:
GENUINE / PROBABLY_FAKE / FAKE.

Privacy: Google's free tier says "do not submit personal information". So by
default Gemini gets (1) file forensics and (2) the payslip's salary lines with
names, employee ID, PAN, UAN, PF / bank / Aadhaar numbers and addresses removed
here first (redact_for_ai) — amounts only, nothing that identifies the person.
The full PDF (needed for the visual check) is sent only when GEMINI_SEND_PDF=1,
which must be set only after billing is enabled on the Google project.
"""

import base64
import calendar
import io
import itertools
import json
import math
import os
import re
import time
from datetime import datetime, timedelta, timezone

import requests
import urllib3
from pypdf import PdfReader, PdfWriter

from ..config import get_settings

_S = get_settings()
GEMINI_API_KEY = _S.gemini_api_key.strip()
GEMINI_MODEL = _S.gemini_model.strip()
# Free tier often returns 503 "high demand" on the main model — fall back to Flash-Lite
GEMINI_FALLBACK_MODEL = _S.gemini_fallback_model.strip()
GEMINI_SEND_PDF = _S.gemini_send_pdf
GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
GEMINI_DEADLINE_S = _S.gemini_deadline_s  # per model attempt, then fall back


class PayslipPasswordError(Exception):
    pass


class NotPayslipError(Exception):
    def __init__(self, doc_type: str, reason: str):
        super().__init__(reason)
        self.doc_type, self.reason = doc_type, reason


# ─── Indian payroll rules (update when the law changes) ──────
# Pay months are compared as "YYYY-MM" strings.
LABOUR_CODES_FROM = "2025-12"     # Labour Codes in force 21 Nov 2025: basic+DA >= 50% of pay
PF_RATE = 0.12
PF_CEILING_OLD, PF_CEILING_NEW = 15000, 25000
PF_CEILING_NEW_FROM = "2026-09"   # EPF wage ceiling raised to ₹25,000 from 17 Sep 2026
ESI_LIMIT, ESI_RATE = 21000, 0.0075
PT_ANNUAL_CAP = 2500              # Article 276 of the Constitution
TAX_FREE_FROM = "2025-04"         # new regime: ₹12L rebate + ₹75k standard deduction
TAX_FREE_SALARY, TAX_FREE_SALARY_OLD = 1275000, 775000
# Credit policy, not law (PF is legally optional above the wage ceiling) — genuine high salaries show PF
PF_REQUIRED_ABOVE = 100000        # gross per month

# UAN and PF number are read from the PDF text locally, so they work on the free tier
UAN_RE = re.compile(r"\bUAN\b(?:\s*(?:No\.?|Number|#))?\s*[:\-–]?\s*(\d{4}\s\d{4}\s\d{4}|\d{3}\s\d{3}\s\d{3}\s\d{3}|\d+)", re.I)
PF_RE = re.compile(r"\b(?:E\.?P\.?F|P\.?F)\.?\s*(?:A/?C|Account|Acct\.?|Member\s*ID)?\s*(?:No\.?|Number|#)?\s*[:\-–]?\s*"
                   r"([A-Z]{2}[\s/\-]*[A-Z]{3}[\s/\-]*\d[\d\s/\-]{2,30}\d)", re.I)
PF_NUM_RE = re.compile(r"\b(?:E\.?P\.?F|P\.?F)\.?\s*(?:A/?C|Account|Acct\.?)?\s*(?:No\.?|Number)\s*[:\-–]?\s*(\d{4,12})\b", re.I)

# ─── Redaction: what may leave the server on the free tier ───
# Lines carrying identity fields are dropped whole
IDENTITY_LINE = re.compile(
    r"\b(name|employee\s*(id|code|no)|emp\.?\s*(id|code|no)|(staff|person|associate|member|candidate)\s*(id|no|code)|pan|uan|"
    r"(e?pf|p\.f\.)\s*(no|number|a/?c|account|member)|pension\s*(no|number|id)|esic?\s*(no|number|ip)|ip\s*no|bank|a/?c\s*no|account|code|"
    r"ifsc|aadhaa?r|address|dob|date\s*of\s*(birth|joining)|doj|father|spouse|e-?mail|mobile|phone|contact)\b", re.I)
# Only lines with salary words (or bare amounts) are kept
PAYROLL_LINE = re.compile(
    r"\b(basic|hra|house\s*rent|d\.?a|dearness|allowances?|allow|allw|alw|conveyance|special|bonus|incentive|overtime|arrears?|lta|"
    r"leave|medical|gross|earnings?|deductions?|e?pf|provident|vpf|esic?|professional|p\.?\s?tax|pt|tds|income\s*tax|"
    r"tax|net|take\s*home|total|rupees|only|salary|pay\s*slip|payslip|month|period|days|lop|attendance|absent|present|ctc|reimbursement|"
    r"loan|advance|insurance|lwf|welfare|nps|amount|rs|inr)\b", re.I)
AMOUNT_LINE = re.compile(r"[\d\s,.\-₹:()/]+")
# Any line carrying a rupee amount is a salary/tax line (identity lines are dropped first) —
# label words vary too much ("Skill Proficiency Allow.") to rely on keywords alone
MONEY = re.compile(r"\d[\d,]*\.\d{2}\b|\b\d{1,3}(?:,\d{2,3})+\b")  # 1,234.50 or Indian-grouped 1,11,424
# Salary labels that happen to contain an identity word ("Mobile & Internet Allowance", "Bank Loan Deduction")
SALARY_LABEL = re.compile(r"allowance|\ballow\b|reimb|deduction|recovery|bonus|incentive|arrear|\bbill\b|insurance|premium", re.I)
# Codes mixing letters and digits (employee codes, member IDs) — masked before anything is sent
ALNUM_ID = re.compile(r"\b(?=[A-Za-z0-9-]*\d)(?=[A-Za-z0-9-]*[A-Za-z])[A-Za-z0-9-]{5,}\b")
MONTH_TOKEN = re.compile(r"(jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*-?\d{2,4}", re.I)
# PF numbers from exempted PF trusts (e.g. "EPF Trust-GN/GGN/5572/362628") don't follow the EPFO label layout
PF_SLASH_RE = re.compile(r"\b([A-Z]{2}/[A-Z]{3}/\d{1,7}(?:/\d{1,3})?/\d{1,7})\b")
NAME_STOPWORDS = {"employee", "name", "mr", "mrs", "ms", "shri", "smt", "dr", "the"}

# ─── Is it a payslip? (checked before any fraud check) ───────
PAYSLIP_SIGNS = {
    "title": re.compile(r"\b(pay\s*slip|payslip|salary\s*slip|pay\s*stub|salary\s*statement|pay\s*statement|wage\s*slip)\b", re.I),
    "earnings": re.compile(r"\b(earnings?|gross\s*(salary|pay|earnings?))\b", re.I),
    "deductions": re.compile(r"\bdeductions?\b", re.I),
    "net pay": re.compile(r"\b(net\s*(pay|salary|amount\s*payable)|take\s*home)\b", re.I),
    "salary components": re.compile(r"\b(basic|h\.?r\.?a|house\s*rent|provident\s*fund|professional\s*tax|special\s*allowance|conveyance)\b", re.I),
}
OTHER_DOCS = {
    "bank statement": re.compile(r"\b(opening\s*balance|closing\s*balance|statement\s*of\s*account|account\s*statement|value\s*date|cheque\s*no|withdrawals?)\b", re.I),
    "Form 16": re.compile(r"\b(form\s*(no\.?\s*)?16|section\s*203|tds\s*certificate)\b", re.I),
    "offer / appointment letter": re.compile(r"\b(offer\s*letter|letter\s*of\s*(offer|appointment)|appointment\s*letter|we\s*are\s*pleased|pleased\s*to\s*offer)\b", re.I),
    "salary certificate": re.compile(r"\b(salary\s*certificate|to\s*whom\s*it\s*may\s*concern|this\s*is\s*to\s*certify)\b", re.I),
    "invoice": re.compile(r"\b(tax\s*invoice|invoice\s*(no|number|date)|bill\s*to)\b", re.I),
    "income tax return": re.compile(r"\b(income\s*tax\s*return|itr-?v|acknowledgement\s*number)\b", re.I),
    "ID document": re.compile(r"\b(unique\s*identification\s*authority|election\s*commission|driving\s*licen[cs]e|republic\s*of\s*india)\b", re.I),
}
# What Gemini may call the document when the local scan is unsure
DOC_TYPES = {
    "PAYSLIP": "payslip", "BANK_STATEMENT": "bank statement", "FORM_16": "Form 16",
    "OFFER_LETTER": "offer / appointment letter", "SALARY_CERTIFICATE": "salary certificate",
    "INVOICE": "invoice", "ID_DOCUMENT": "ID document", "OTHER": "other document",
}
# A value runs until the next known payslip label on the same line
_NEXT_LABEL = (r"(?:emp(?:loyee)?\.?\s*(?:code|id|no|name)|staff\s*(?:id|no)|name|designation|department|dept|grade|"
               r"location|branch|pan|uan|e?pf\s*(?:no|a/?c|account)|esic?\s*(?:no|ip)|bank|a/?c\s*no|ifsc|"
               r"date\s*of\s*(?:joining|birth)|doj|dob|gender|days|month|mobile|phone|e-?mail)")
# Separator is ":" / "-", or (in layout text) a gap of 2+ spaces between label and value columns
LABEL_VALUE = r"{label}(?:\s*[:\-–]\s*|\s{{2,}})(.+?)(?=\s{{2,}}|\s+" + _NEXT_LABEL.replace("{", "{{").replace("}", "}}") + r"\s*[:\-–]|$)"
# "Name" only at the start of a line or column, so "Bank Name" / "Father's Name" aren't read as the employee
NAME_LABEL = r"(?:^|(?<=\s\s))(?:employee\s*|emp\.?\s*)?name"
# Report generators that never record creation dates — a missing date is normal for them
REPORT_TOOLS = ["crystal", "jasper", "reporting services", "ssrs", "birt", "bi publisher", "stimulsoft", "telerik", "devexpress"]


# ─── Producer / creator signatures ───────────────────────────
# Fake-payslip generator sites
GENERATOR_SITES = [
    "payslip generator", "salary slip generator", "payslipgenerator", "paystub",
    "pay stub", "stubcreator", "123paystubs", "formswift", "template.net",
]
# PDF / design editors — the file was made or changed by a person
EDITOR_TOOLS = [
    "pdfescape", "pdf-xchange", "pdfxchange", "foxit phantom", "foxit pdf editor",
    "foxit editor", "nitro", "canva", "photoshop", "illustrator", "inkscape",
    "pdfelement", "wondershare", "pdffiller", "dochub", "xodo", "pdf expert",
    "acrobat pro", "acrobat standard",
]
# Online multi-tools people use to remove a payslip's password. Re-saving wipes the
# original metadata, so the file's origin can't be verified -> NEEDS_REVIEW, not fake
UNLOCK_TOOLS = [
    "ilovepdf", "smallpdf", "sejda", "sodapdf", "soda pdf", "pdf24", "online2pdf",
    "pdfcandy", "pdf candy", "lightpdf", "hipdf", "pdfgear", "pdf2go", "qpdf", "pdftk", "unlock",
]
# Small employers genuinely make payslips in these — so only a warning
OFFICE_TOOLS = [
    "microsoft word", "microsoft® word", "word for microsoft", "microsoft excel",
    "microsoft® excel", "libreoffice", "openoffice", "wps writer", "wps office",
    "wps spreadsheets", "google docs", "google sheets",
]
# Printed / re-saved from a browser or OS — not the HR portal's original file
PRINT_TOOLS = [
    "skia/pdf", "chrome", "chromium", "microsoft: print to pdf",
    "microsoft print to pdf", "quartz pdfcontext", "mac os x",
]
# Indian payroll / HRMS software — a positive sign, not proof
PAYROLL_TOOLS = [
    "keka", "greythr", "zoho", "darwinbox", "successfactors", "workday", "adp",
    "paybooks", "razorpay", "hrone", "sumhr", "zimyo", "spine", "saral", "tally",
    "peoplesoft", "ramco", "factohr", "pockethrms", "qandle", "kredily", "sap",
]
# Overlay annotation types used to cover and rewrite amounts
OVERLAY_ANNOTS = {"/FreeText", "/Square", "/Stamp", "/Ink", "/Redact", "/Polygon"}


def _match(tools: str, names: list[str]) -> str | None:
    for name in names:
        if re.search(r"(?<![a-z])" + re.escape(name) + r"(?![a-z])", tools):
            return name
    return None


def _check(id_: str, label: str, status: str, detail: str) -> dict:
    return {"id": id_, "label": label, "status": status, "detail": detail}


def _inr(x: float) -> str:
    """₹ with Indian digit grouping: 120000 -> ₹1,20,000."""
    s = f"{abs(x):.0f}"
    if len(s) > 3:
        s = re.sub(r"(\d)(?=(\d\d)+$)", r"\1,", s[:-3]) + "," + s[-3:]
    return ("-" if x < 0 else "") + "₹" + s


def _lakh(x: float) -> str:
    return f"₹{x / 100000:.2f} lakh"


def find_ids(text: str) -> dict:
    """UAN (always 12 digits) and PF account / member ID printed on the payslip."""
    uan = None
    m = UAN_RE.search(text)
    if m:
        uan = re.sub(r"\D", "", m.group(1))
    elif re.search(r"\bUAN\b", text, re.I):
        # label and value split by the PDF layout — UANs are allotted from 10xxxxxxxxxx
        m = re.search(r"(?<!\d)10\d{10}(?!\d)", text)
        uan = m.group(0) if m else None
    m = PF_RE.search(text) or PF_NUM_RE.search(text)
    if not m and re.search(r"\b(e?pf|provident|pension)\b", text, re.I):
        m = PF_SLASH_RE.search(text)
    return {"uan": uan, "pf_number": m.group(1).strip() if m else None}


LABEL_WORDS = {"pay", "period", "paid", "days", "date", "name", "employee", "emp", "id", "code", "designation", "department",
               "month", "location", "bank", "account", "no", "number", "uan", "pan", "lop", "doj", "joining", "gender", "grade",
               "band", "branch", "ifsc", "esi", "pf", "of", "a/c", "&"}


def _label_value(text: str, label: str) -> str | None:
    """Value printed after a label — but never another label (table headers put labels side by side)."""
    for m in re.finditer(LABEL_VALUE.format(label=label), text, re.I | re.M):
        v = m.group(1).strip()
        if v and not all(w.lower().strip(":.") in LABEL_WORDS for w in v.split()):
            return v
    return None


COMPANY_RE = re.compile(r"\b(ltd|limited|pvt|private|llp|inc|corporation)\b", re.I)


def _employer(text: str) -> str | None:
    """The segment carrying a company suffix, skipping bank / address lines."""
    for line in text.splitlines():
        if IDENTITY_LINE.search(line):
            continue
        for seg in re.split(r"\s{2,}", line.strip()):
            if COMPANY_RE.search(seg):
                return seg.strip()
    return None


def page_header(text: str) -> dict:
    """Employer / employee details read locally — never sent to Gemini.
    Expects layout-preserving text so labels and values stay on one line."""
    return {
        "employer_name": _employer(text),
        "employee_name": _label_value(text, NAME_LABEL),
        "employee_id": _label_value(text, r"(?:employee|emp\.?|staff)\s*(?:id|code|no\.?)"),
        "designation": _label_value(text, r"designation"),
        **find_ids(text),
    }


SEGMENT = re.compile(r"\S+(?: \S+)*")  # a column in layout text: words separated by single spaces


def _blank_value_rows(text: str) -> str:
    """Table-style payslips print labels in one row and the values in the row below
    ("EMPLOYEE NAME   EMPLOYEE ID" over "<name>   <id>"). Blank the values sitting under
    identity labels, by column position, so they can't reach the AI."""
    lines = text.splitlines()
    for i, line in enumerate(lines[:-1]):
        segs = list(SEGMENT.finditer(line))
        if len(segs) < 2 or re.search(r"\d", line):  # a header row holds labels only
            continue
        cols = [(s.start(), segs[k + 1].start() if k + 1 < len(segs) else len(line) + 40)
                for k, s in enumerate(segs) if IDENTITY_LINE.search(s.group()) and not SALARY_LABEL.search(s.group())]
        j = next((j for j in range(i + 1, min(i + 3, len(lines))) if lines[j].strip()), None)
        if not cols or j is None or IDENTITY_LINE.search(lines[j]):
            continue
        row = list(lines[j])
        for a, b in cols:
            row[a:min(b, len(row))] = " " * max(0, min(b, len(row)) - a)
        lines[j] = "".join(row)
    return "\n".join(lines)


def redact_for_ai(text: str) -> str:
    """Keep only salary lines; drop identity fields and mask anything that identifies the person.

    Works per column: in layout text a row like "Name   <value>   Payable Days   31.00"
    loses only the name pair, so the attendance on the same row still reaches the AI."""
    text = _blank_value_rows(text)
    names = set()
    for v in re.findall(LABEL_VALUE.format(label=NAME_LABEL), text, re.I | re.M):
        names |= {w for w in re.findall(r"[A-Za-z]{3,}", v) if w.lower() not in NAME_STOPWORDS | LABEL_WORDS}
    kept, skip_next = [], False
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        if skip_next:  # value printed on the line after its "Label:"
            skip_next = False
            continue
        segs = re.split(r"\s{2,}", line)
        out, drop_next = [], False
        for seg in segs:
            if not seg.strip(" :-–/|"):  # a lone ":" column belongs to the label before it
                continue
            if drop_next and not PAYROLL_LINE.search(seg):  # the identity label's value column
                drop_next = False
                continue
            drop_next = False
            if IDENTITY_LINE.search(seg) and not SALARY_LABEL.search(seg):
                # A bare label ("Name", "PAN :") has its value in the next column
                drop_next = not re.search(r"[\d@]", seg) and len(seg.split()) <= 5
                continue
            out.append(seg)
        if not out:
            skip_next = len(segs) == 1 and bool(re.search(r"[:\-–]\s*$", line))
            continue
        line = "  ".join(out)
        if PAYROLL_LINE.search(line) or AMOUNT_LINE.fullmatch(line) or MONEY.search(line):
            kept.append(line)
    out = "\n".join(kept)
    for w in names:
        out = re.sub(rf"\b{re.escape(w)}\b", "[NAME]", out, flags=re.I)
    out = re.sub(r"\b[A-Z]{5}\d{4}[A-Z]\b", "[PAN]", out)
    out = re.sub(r"\S+@\S+\.\S+", "[EMAIL]", out)
    out = re.sub(r"\b\d{4}\s\d{4}\s\d{4}\b", "[ID]", out)
    out = re.sub(r"(?<![\d,.])\d{9,}(?![\d,.])", "[ID]", out)  # account / UAN / Aadhaar / phone, not amounts
    out = PF_SLASH_RE.sub("[ID]", out)                           # PF numbers that slipped past the line filter
    out = ALNUM_ID.sub(lambda m: m.group(0) if MONTH_TOKEN.fullmatch(m.group(0)) else "[ID]", out)  # employee codes
    out = re.sub(r"\b[A-Z]{5}\d{17}\b", "[ID]", out)
    return out


def _a(noun: str) -> str:
    return ("an " if noun[0].lower() in "aeiou" or noun.startswith("ID") else "a ") + noun


def classify_document(text: str) -> dict:
    """Keyword scan: "payslip", "other" (clearly not a payslip), "unsure" or "unknown" (no text)."""
    signs = [k for k, rx in PAYSLIP_SIGNS.items() if rx.search(text)]
    others = {name: sorted({m.group(0).lower() for m in rx.finditer(text)}) for name, rx in OTHER_DOCS.items()}
    others = {k: v for k, v in others.items() if v}
    other = max(others, key=lambda k: len(others[k]), default=None)
    title = "title" in signs
    core = {"earnings", "deductions", "net pay"} & set(signs)
    if not text.strip():
        verdict = "unknown"
    elif title and len(signs) >= 3:
        verdict = "payslip"
    elif len(core) >= 2 and "salary components" in signs and not other:
        verdict = "payslip"
    elif (other or len(signs) <= 1) and not title:
        verdict = "other"
    else:
        verdict = "unsure"
    return {"verdict": verdict, "signs": signs, "other_type": other, "other_terms": others.get(other, [])}


def _doc_check(doc: dict, ai_type: str | None) -> dict:
    if doc["verdict"] == "payslip":
        return _check("doc", "Document type", "pass", "Payslip — found " + ", ".join(doc["signs"]) + ".")
    if ai_type == "PAYSLIP":
        return _check("doc", "Document type", "pass", "Payslip (confirmed by AI — the layout is unusual).")
    return _check("doc", "Document type", "warn", "Couldn't confirm this is a payslip (scanned or unusual layout) — check it's a salary slip.")


def _id_check(ids: dict, pf_deducted: bool | None) -> dict:
    """Details are masked to the last 4 digits — they go into the AI evidence."""
    uan, pf_no = ids.get("uan"), ids.get("pf_number")
    if uan and len(uan) != 12:
        return _check("ids", "UAN / PF No.", "fail", f"UAN has {len(uan)} digits — a real UAN is always 12 digits.")
    parts = []
    if uan:
        parts.append(f"UAN present (12 digits, ends {uan[-4:]})")
    if pf_no:
        norm = re.sub(r"[\s/\-]", "", pf_no).upper()
        kind = ("full member ID" if re.fullmatch(r"[A-Z]{5}\d{17}", norm)
                else "region code + number" if norm[:5].isalpha() else "number only")
        parts.append(f"PF number present ({kind}, ends {norm[-4:]})")
    if parts:
        return _check("ids", "UAN / PF No.", "pass", "; ".join(parts) + ".")
    if pf_deducted:
        return _check("ids", "UAN / PF No.", "warn", "PF is deducted but no UAN or PF number is printed — real payroll payslips show them.")
    if pf_deducted is False:
        return _check("ids", "UAN / PF No.", "warn", "No UAN or PF number and no PF deducted — fine only if the employer isn't PF-registered (under 20 staff).")
    return _check("ids", "UAN / PF No.", "warn", "No UAN or PF number found on the payslip.")


def _fmt_date(d: datetime | None) -> str | None:
    return d.strftime("%d %b %Y, %H:%M") if d else None


def _safe_date(getter):
    try:
        d = getter()
    except Exception:
        return None
    if d and d.tzinfo is None:
        d = d.replace(tzinfo=timezone.utc)
    return d


def _font_family(base_font: str) -> str:
    name = base_font.lstrip("/").split("+", 1)[-1]  # drop subset prefix ABCDEF+
    family = re.split(r"[-,]", name)[0]
    return re.sub(r"(PSMT|MT|PS)$", "", family) or name


def _open(pdf_bytes: bytes, password: str) -> PdfReader:
    reader = PdfReader(io.BytesIO(pdf_bytes))
    if reader.is_encrypted:
        # Many PDFs only have an owner password — try empty first
        if not reader.decrypt("") and not (password and reader.decrypt(password)):
            raise PayslipPasswordError()
    return reader


# ─── Local forensics ─────────────────────────────────────────

def pdf_forensics(pdf_bytes: bytes, reader: PdfReader, filename: str = "") -> dict:
    info = reader.metadata or {}
    created = _safe_date(lambda: info.creation_date)
    modified = _safe_date(lambda: info.modification_date)

    # XMP keeps the tool history even when the Info dictionary is rewritten
    xmp_agents = []
    try:
        xmp = reader.root_object.get("/Metadata")
        if xmp is not None:
            xml = xmp.get_object().get_data().decode("utf-8", "ignore")
            xmp_agents = re.findall(r'(?:softwareAgent|CreatorTool|Producer)(?:>|=")([^<"]+)', xml)
    except Exception:
        pass

    # Each incremental save appends a new %%EOF; linearized files legitimately have 2
    eof_count = pdf_bytes.count(b"%%EOF")
    linearized = b"/Linearized" in pdf_bytes[:2048]
    saves = max(1, eof_count - (1 if linearized and eof_count > 1 else 0))

    fonts_by_page, overlays, texts, layout_texts = [], [], [], []
    for i, page in enumerate(reader.pages):
        fams = set()
        try:
            fonts = page.get("/Resources", {}).get_object().get("/Font", {}).get_object()
            for f in fonts.values():
                base = f.get_object().get("/BaseFont")
                if base:
                    fams.add(_font_family(str(base)))
        except Exception:
            pass
        fonts_by_page.append(fams)
        annots = page.get("/Annots")
        for a in (annots.get_object() if annots is not None else []):
            try:
                sub = a.get_object().get("/Subtype")
                if sub in OVERLAY_ANNOTS:
                    overlays.append(f"page {i + 1}: {str(sub).lstrip('/')}")
            except Exception:
                pass
        if i < 12:
            try:
                texts.append(page.extract_text() or "")
                layout_texts.append(page.extract_text(extraction_mode="layout") or "")
            except Exception:
                pass
    text = "\n".join(texts)

    all_fonts = sorted(set().union(*fonts_by_page)) if fonts_by_page else []
    producer = str(info.get("/Producer") or "").strip()
    creator = str(info.get("/Creator") or "").strip()
    title = str(info.get("/Title") or "").strip()
    author = str(info.get("/Author") or "").strip()
    tools = " | ".join([producer, creator, title, author, *xmp_agents]).lower()

    metadata = {
        "producer": producer or None,
        "creator": creator or None,
        "author": author or None,
        "title": title or None,
        "xmp_tools": sorted(set(xmp_agents)) or None,
        "created": _fmt_date(created),
        "modified": _fmt_date(modified),
        "pages": len(reader.pages),
        "pdf_version": reader.pdf_header.lstrip("%"),
        "encrypted": reader.is_encrypted,
        "saves": saves,
        "fonts": all_fonts,
        "has_text": bool(text.strip()),
        "size_kb": round(len(pdf_bytes) / 1024, 1),
    }

    checks = []

    # 1. Which software made the file
    generator = _match(tools, GENERATOR_SITES)
    editor = _match(tools, EDITOR_TOOLS)
    office = _match(tools, OFFICE_TOOLS)
    payroll = _match(tools, PAYROLL_TOOLS)
    report = _match(tools, REPORT_TOOLS)
    resaver = _match(tools, UNLOCK_TOOLS) or _match(tools, PRINT_TOOLS)
    name_hint = bool(re.search(r"unlock|decrypt|unprotect|no[_\- ]?pass|without[_\- ]?pass", filename, re.I))
    unlocked = None
    if generator:
        checks.append(_check("tool", "Software", "fail", f"Made with a payslip generator ({generator}) — not issued by an employer."))
    elif editor:
        checks.append(_check("tool", "Software", "fail", f"Made or re-saved with a PDF/design editor ({editor}) — payroll systems don't use this."))
    elif office:
        # The authoring app matters more than an online converter it later passed through
        via = f", then converted with {next((t for t in (producer, creator) if resaver in t.lower()), resaver)}" if resaver else ""
        checks.append(_check("tool", "Software", "warn", f"Made in an office app ({office}{via}) — small employers do this, but so do fakes. Match net pay with the salary credit in the bank statement."))
    elif resaver or (name_hint and not producer and not creator):
        # Show the tool as the PDF names it ("iLovePDF"), not our lowercase match
        shown = next((t for t in (producer, creator) if resaver and resaver in t.lower()), resaver)
        unlocked = {"tool": shown, "filename_hint": name_hint, "printed": bool(_match(tools, PRINT_TOOLS))}
        how = f"printed to PDF ({shown})" if unlocked["printed"] else f"re-saved with {shown}" if shown else "re-saved"
        checks.append(_check("tool", "Unlocked / re-saved", "review",
                             f"The file was {how}{' and its name says it was unlocked' if name_hint else ''} — usually done to remove the password. "
                             "That erases the original PDF details (which software made it, and when), so the file's origin can't be verified."))
    elif payroll:
        checks.append(_check("tool", "Software", "pass", f"Generated by payroll software ({payroll})."))
    elif report:
        checks.append(_check("tool", "Software", "pass", f"Generated by a report system ({producer or creator}) — typical of payroll exports."))
    elif not producer and not creator:
        checks.append(_check("tool", "Software", "warn", "No producer/creator in metadata — often stripped by editing tools."))
    else:
        checks.append(_check("tool", "Software", "pass", f"No editing tool found ({producer or creator})."))

    # 2. Modified after creation. Generators often write wrong/missing timezones
    # (up to ±14h), so small gaps are tolerated.
    now = datetime.now(timezone.utc)
    gap_h = (modified - created).total_seconds() / 3600 if created and modified else 0
    if unlocked:
        checks.append(_check("dates", "Dates", "review", "Rewritten when the file was unlocked / re-saved — the original dates can't be checked."))
    elif created and created > now + timedelta(days=1):
        checks.append(_check("dates", "Dates", "fail", "Creation date is in the future — metadata was faked."))
    elif gap_h < -14:
        checks.append(_check("dates", "Dates", "fail", "Modified date is before creation date — metadata was tampered."))
    elif gap_h > 24:
        checks.append(_check("dates", "Dates", "fail", f"Modified {int(gap_h // 24)} day(s) after it was created — edited after it was generated."))
    elif gap_h > 1:
        checks.append(_check("dates", "Dates", "warn", f"Modified {int(gap_h)} hour(s) after creation — may have been edited."))
    elif not created and report:
        checks.append(_check("dates", "Dates", "pass", f"No dates recorded — normal for {producer or creator} exports."))
    elif not created:
        checks.append(_check("dates", "Dates", "warn", "No creation date in metadata."))
    else:
        checks.append(_check("dates", "Dates", "pass", "Not modified after creation."))

    # 3. Incremental saves
    if saves > 1:
        checks.append(_check("saves", "Save history", "warn", f"File was saved {saves} times — content may have changed after generation."))
    else:
        checks.append(_check("saves", "Save history", "pass", "Saved once, as generated."))

    # 4. Overlay annotations (white box + new text over an amount)
    if overlays:
        checks.append(_check("overlays", "Overlays", "fail", "Boxes/text drawn on top of the payslip: " + ", ".join(overlays[:5])))
    else:
        checks.append(_check("overlays", "Overlays", "pass", "No overlay boxes or added text."))

    # 5. Fonts — edits often bring in a font the payroll template never uses
    odd = []
    if len(fonts_by_page) >= 3:
        for fam in all_fonts:
            pages = [i for i, f in enumerate(fonts_by_page) if fam in f]
            if len(pages) == 1 and 0 < pages[0] < len(fonts_by_page) - 1:
                odd.append(f"{fam} (page {pages[0] + 1})")
    if odd:
        checks.append(_check("fonts", "Fonts", "warn", "Font used on only one page: " + ", ".join(odd)))
    elif len(all_fonts) > 4:
        checks.append(_check("fonts", "Fonts", "warn", f"{len(all_fonts)} font families — payroll templates usually use 1–3."))
    else:
        checks.append(_check("fonts", "Fonts", "pass", f"{len(all_fonts)} font famil{'y' if len(all_fonts) == 1 else 'ies'}, consistent."))

    # 6. Text layer
    if metadata["has_text"]:
        checks.append(_check("text", "Text layer", "pass", "Digital PDF with selectable text."))
    else:
        checks.append(_check("text", "Text layer", "warn", "Scanned / photo — can't be checked digitally. Ask for the PDF from the HR portal."))

    return {"metadata": metadata, "checks": checks, "created": created, "text": text, "layout_texts": layout_texts, "unlocked": unlocked}


# ─── Payslip maths (full-PDF mode only) ──────────────────────

def _total(items: list[dict] | None, kinds: set[str] | None = None) -> float:
    return sum((i.get("amount") or 0) for i in items or [] if kinds is None or i.get("type") in kinds)


def _month_label(ym: str | None) -> str:
    try:
        return datetime.strptime(ym or "", "%Y-%m").strftime("%b %Y")
    except ValueError:
        return ym or "?"


def _fix_crossed_columns(items: list[dict], gross: float) -> list[dict] | None:
    """Payslips with Standard and Actual columns can come out of PDF text in a jumbled order,
    and the AI may take the standard figure for some rows. Try each row's two printed values;
    return the rows using the combination that adds up to the printed gross, else None.
    Only printed numbers are used and no row is dropped, so an invented allowance still fails."""
    options = [sorted({i.get("amount") or 0} | ({i["standard"]} if i.get("standard") else set())) for i in items]
    if len(items) > 12 or all(len(o) == 1 for o in options):
        return None
    for combo in itertools.product(*options):
        if abs(sum(combo) - gross) <= 2:
            return [{**i, "amount": v, "standard": max(i.get("standard") or 0, i.get("amount") or 0)}
                    for i, v in zip(items, combo)]
    return None


def _payslip_checks(slips: list[dict], created: datetime | None) -> tuple[list[dict], list[dict]]:
    """Python re-does every sum on the payslip — an edited amount breaks at least one."""
    if not slips:
        return [_check("totals", "Totals", "warn", "Couldn't read a payslip from this PDF — check it manually.")], []

    found = {k: [] for k in ("totals", "words", "basic", "hra", "pf", "esi", "pt", "tds", "month", "days", "consistency")}
    seen = set()
    today = datetime.now(timezone.utc).date()
    summary = []

    for s in slips:
        m = _month_label(s.get("pay_month"))
        earn, ded = s.get("earnings") or [], s.get("deductions") or []
        gross_p, ded_p, net = s.get("gross_earnings") or 0, s.get("total_deductions") or 0, s.get("net_pay") or 0
        if gross_p and earn and abs(_total(earn) - gross_p) > 2:
            earn = _fix_crossed_columns(earn, gross_p) or earn
        e_sum, d_sum = _total(earn), _total(ded)
        gross, total_ded = gross_p or e_sum, ded_p or d_sum
        basic, da, hra = _total(earn, {"BASIC"}), _total(earn, {"DA"}), _total(earn, {"HRA"})
        pf, esi, pt, tds = _total(ded, {"PF"}), _total(ded, {"ESI"}), _total(ded, {"PT"}), _total(ded, {"TDS"})
        ym = s.get("pay_month") or ""
        wage = basic + da  # "wages" for PF / Labour Codes

        seen.add("totals")
        if gross_p and earn and abs(e_sum - gross_p) > 2:
            found["totals"].append(("fail", f"{m}: earnings add to {_inr(e_sum)} but gross shows {_inr(gross_p)}"))
        if ded_p and ded and abs(d_sum - ded_p) > 2:
            found["totals"].append(("fail", f"{m}: deductions add to {_inr(d_sum)} but total shows {_inr(ded_p)}"))
        if net and gross and abs(gross - total_ded - net) > 2:
            found["totals"].append(("fail", f"{m}: {_inr(gross)} − {_inr(total_ded)} = {_inr(gross - total_ded)}, but net pay shows {_inr(net)}"))

        words = s.get("net_pay_in_words_value") or 0
        if words and net:
            seen.add("words")
            if abs(words - net) > 1:
                found["words"].append(("fail", f"{m}: net pay in words says {_inr(words)} but the figure is {_inr(net)} — the figure was likely edited"))

        # Labour Codes: allowances above 50% of pay count as wages, so basic+DA is kept >= 50%
        if gross and wage:
            seen.add("basic")
            share = wage / gross
            if ym >= LABOUR_CODES_FROM and share < 0.49:
                found["basic"].append(("warn", f"{m}: basic + DA is {share:.0%} of gross — the Labour Codes (from 21 Nov 2025) expect at least 50%"))
            elif share < 0.25:
                found["basic"].append(("warn", f"{m}: basic is only {share:.0%} of gross — unusually low"))

        # HRA is normally 40% (non-metro) to 50% (metro) of basic — the income-tax exemption limits
        if hra and basic:
            seen.add("hra")
            if hra > basic:
                found["hra"].append(("warn", f"{m}: HRA {_inr(hra)} is more than basic {_inr(basic)} — very unusual"))
            elif hra / basic > 0.51:
                found["hra"].append(("warn", f"{m}: HRA is {hra / basic:.0%} of basic — normally 40% (non-metro) to 50% (metro)"))

        # PF: 12% of wages, optionally capped at the wage ceiling (₹25,000 from Sep 2026)
        ceiling = PF_CEILING_NEW if ym >= PF_CEILING_NEW_FROM else PF_CEILING_OLD
        if not pf and gross > PF_REQUIRED_ABOVE:
            seen.add("pf")
            found["pf"].append(("fail", f"{m}: no PF deducted on gross pay of {_inr(gross)} — credit policy requires PF above {_inr(PF_REQUIRED_ABOVE)}/month"))
        elif wage:
            code_wage = max(wage, 0.5 * gross) if ym >= LABOUR_CODES_FROM else wage
            caps = {PF_CEILING_OLD, ceiling}  # payroll systems may still cap at ₹15,000 during the switch
            options = {PF_RATE * b for b in (basic, wage, code_wage)} | {PF_RATE * min(b, c) for b in (wage, code_wage) for c in caps}
            if pf:
                seen.add("pf")
                if not any(abs(pf - x) <= 2 for x in options):
                    cap_text = " / ".join(_inr(PF_RATE * c) for c in sorted(caps))
                    found["pf"].append(("warn", f"{m}: PF {_inr(pf)} isn't 12% of basic + DA ({_inr(wage)}) or the {cap_text} cap — unusual unless it's VPF"))
            elif wage <= ceiling:
                seen.add("pf")
                found["pf"].append(("warn", f"{m}: no PF deducted though basic + DA ({_inr(wage)}) is within the {_inr(ceiling)} PF ceiling — mandatory if the employer has 20+ staff"))

        if esi:
            seen.add("esi")
            if gross > ESI_LIMIT:
                found["esi"].append(("warn", f"{m}: ESI deducted but gross {_inr(gross)} is above the {_inr(ESI_LIMIT)} ESI limit"))
            elif abs(esi - math.ceil(ESI_RATE * gross)) > 2:
                found["esi"].append(("warn", f"{m}: ESI {_inr(esi)} isn't 0.75% of gross ({_inr(ESI_RATE * gross)})"))

        if pt:
            seen.add("pt")
            if pt > PT_ANNUAL_CAP:
                found["pt"].append(("fail", f"{m}: professional tax {_inr(pt)} — legally capped at ₹2,500 a year"))

        # TDS: mandatory once salary crosses the tax-free limit (Income-tax law, deducted by the employer)
        if gross:
            seen.add("tds")
            limit = TAX_FREE_SALARY if ym >= TAX_FREE_FROM else TAX_FREE_SALARY_OLD
            if gross * 12 > limit and not tds:
                found["tds"].append(("fail", f"{m}: salary ≈ {_lakh(gross * 12)}/year but no TDS — TDS is mandatory above {_lakh(limit)}"))

        try:
            pm = datetime.strptime(s.get("pay_month") or "", "%Y-%m").date()
            seen.add("month")
            if pm > today:
                found["month"].append(("fail", f"{m}: pay month is in the future"))
            elif created and (created + timedelta(days=1)).date() < pm:
                found["month"].append(("fail", f"{m}: PDF was created on {created:%d %b %Y}, before this pay month started"))
        except ValueError:
            pm = None

        # Attendance: paid days / total days should be printed and fit the month
        paid, total_days, lop = s.get("paid_days") or 0, s.get("total_days") or 0, s.get("lop_days") or 0
        month_days = calendar.monthrange(pm.year, pm.month)[1] if pm else 31
        seen.add("days")
        if not paid and not total_days:
            found["days"].append(("warn", f"{m}: paid days / total days not printed — payroll payslips show attendance"))
        elif not paid:
            found["days"].append(("warn", f"{m}: total days ({total_days:g}) printed but paid days missing"))
        elif max(paid, total_days) > month_days:
            found["days"].append(("fail", f"{m}: {max(paid, total_days):g} days on the payslip but the month has only {month_days}"))
        elif total_days and paid > total_days:
            found["days"].append(("fail", f"{m}: paid days ({paid:g}) are more than total days ({total_days:g})"))
        elif lop and total_days and abs(total_days - lop - paid) > 0.5:
            found["days"].append(("warn", f"{m}: paid {paid:g} + LOP {lop:g} days doesn't equal total {total_days:g}"))

        # With unpaid days, earned Basic = full-month Basic × paid days / total days (calendar or working)
        std_basic = sum(max(i.get("standard") or 0, i.get("amount") or 0) for i in earn if i.get("type") == "BASIC" and i.get("standard"))
        if paid and total_days and paid < total_days and std_basic and basic:
            expected = [std_basic * paid / d for d in {total_days, month_days}]
            if all(abs(basic - x) > max(2, 0.01 * std_basic) for x in expected):
                found["days"].append(("warn", f"{m}: {paid:g} of {total_days:g} days paid, so Basic should be about "
                                              f"{_inr(expected[0])} (full-month {_inr(std_basic)}), but it shows {_inr(basic)}"))

        summary.append({
            "pay_month": s.get("pay_month"), "employer_name": s.get("employer_name"),
            "employee_name": s.get("employee_name"), "employee_id": s.get("employee_id"),
            "designation": s.get("designation"), "uan": s.get("uan"), "pf_number": s.get("pf_number"),
            "gross": gross, "basic": basic, "da": da, "hra": hra, "wage_share": round(wage / gross, 3) if gross else None,
            "pf": pf, "esi": esi, "pt": pt, "tds": tds, "total_deductions": total_ded,
            "net_pay": net, "net_pay_in_words_value": words,
            "paid_days": paid, "total_days": total_days, "lop_days": lop,
        })

    if len(slips) >= 2:
        # A month with unpaid days should pay less than a full month
        full = [r["gross"] for r in summary if r["paid_days"] and r["total_days"] and r["paid_days"] >= r["total_days"]]
        for r in summary:
            if full and r["paid_days"] and r["total_days"] and r["paid_days"] < r["total_days"] and r["gross"] >= max(full) - 2:
                found["days"].append(("warn", f"{_month_label(r['pay_month'])}: only {r['paid_days']:g} of {r['total_days']:g} days paid "
                                              f"but gross {_inr(r['gross'])} equals a full month — pay not reduced for absence"))

        seen.add("consistency")
        for field, label in (("employer_name", "Employer"), ("employee_id", "Employee ID"), ("uan", "UAN"), ("pf_number", "PF number")):
            vals = {(s.get(field) or "").strip().lower() for s in slips} - {""}
            if len(vals) > 1:
                found["consistency"].append(("warn", f"{label} differs between months"))
        # One pay rise is a normal appraisal; repeated changes or a cut in Basic are not
        by_month = [r["basic"] for r in sorted(summary, key=lambda r: r["pay_month"] or "") if r["basic"]]
        steps = [b - a for a, b in zip(by_month, by_month[1:]) if abs(b - a) > 1]
        if len(steps) > 1 or any(s < 0 for s in steps):
            found["consistency"].append(("warn", "Basic changes between months (" + ", ".join(_inr(b) for b in by_month) + ") — more than one change, or a cut"))

    n = len(slips)
    rules = [
        ("totals", "Totals", f"Earnings, deductions and net pay add up on {n} payslip{'s' if n > 1 else ''}."),
        ("words", "Net pay in words", "Net pay in words matches the figure."),
        ("basic", "Basic share", "Basic + DA is a normal share of gross (≥ 50% under the Labour Codes)."),
        ("hra", "HRA", "HRA is within 40–50% of basic."),
        ("pf", "PF", "PF is 12% of basic + DA or the wage-ceiling cap."),
        ("esi", "ESI", "ESI is 0.75% of gross and within the ₹21,000 limit."),
        ("pt", "Professional tax", "Professional tax is within the ₹2,500/year cap."),
        ("tds", "TDS", "TDS is consistent with the salary level."),
        ("month", "Pay month", "PDF was created after the pay month started."),
        ("days", "Attendance", "Paid days are printed and fit the month."),
        ("consistency", "Consistency", "Employer, employee ID, UAN, PF number and basic match across months."),
    ]
    checks = []
    for key, label, ok_text in rules:
        issues = found[key]
        if issues:
            status = "fail" if any(s == "fail" for s, _ in issues) else "warn"
            checks.append(_check(key, label, status, "; ".join(t for _, t in issues)))
        elif key in seen and ok_text:
            checks.append(_check(key, label, "pass", ok_text))
    return checks, summary


# ─── Gemini ──────────────────────────────────────────────────

REVIEW_PROMPT = """You are a document-fraud analyst at an Indian NBFC checking salary slips (payslips) that loan applicants upload.
Give a verdict: GENUINE, NEEDS_REVIEW, PROBABLY_FAKE or FAKE.
- NEEDS_REVIEW (medium risk) is for a payslip that was unlocked / re-saved (checks with status "review"): the original file details are gone, so its origin can't be verified, but that is NOT evidence of fraud. If nothing else is wrong, answer NEEDS_REVIEW, not PROBABLY_FAKE or FAKE.

Weigh the evidence:
- Strong fake signals: made with payslip-generator sites, PDF editors or design tools (Canva, Photoshop), edited long after creation, overlay boxes or added text, totals or net pay not adding up, net pay in words not matching the figure, PDF created before the pay month, a UAN that isn't 12 digits, professional tax above ₹2,500, no TDS on a salary above ₹12.75 lakh a year (TDS is mandatory), no PF on gross pay above ₹1 lakh a month (our credit policy), more paid days than the month has or than total days.
- Medium signals: made in Word/Excel (small employers do this, but so do fakes), paid days / total days not printed, absence (LOP) not reducing pay, PF deducted with no UAN/PF number printed, PF not 12% of basic + DA, ESI above the ₹21,000 limit, employer/UAN/PF number/basic changing between months.
- Indian payroll rules were checked in Python: Labour Codes (from 21 Nov 2025) expect basic + DA ≥ 50% of pay; HRA is normally 40–50% of basic; PF is 12% of basic + DA, capped at ₹1,800 (₹3,000 since the EPF ceiling rose to ₹25,000 on 17 Sep 2026); ESI is 0.75% and only up to ₹21,000 gross; professional tax ≤ ₹2,500 a year; TDS is due above ₹12.75 lakh a year. An odd salary structure (low basic, high HRA) alone is a weak signal — some employers are simply non-compliant.
- Weak signals (alone = PROBABLY_FAKE at most): missing metadata, extra fonts, multiple saves. Low basic or high HRA alone never makes a payslip fake — big employers use such structures.
- Payroll software (Keka, greytHR, Zoho Payroll, Darwinbox, SAP, etc.) is a positive sign, not proof.
- The checks were computed in Python — trust them, never redo the maths or contradict them.
- Use only the evidence given. Never invent facts. Write short, plain sentences a credit manager can act on. If not GENUINE, end the summary with what to verify next (e.g. match net pay with the salary credit in the bank statement, call the employer's HR)."""

METADATA_PROMPT_EXTRA = """
You only see file forensics, not the payslip itself. GENUINE here means "no tampering found in the file" — give it at most MEDIUM confidence."""

TEXT_PROMPT_EXTRA = """
The salary figures were read from the payslip's text and checked in Python, but nobody looked at the page itself, so there is no visual check."""

# Step 1 of full mode: read the PDF. The verdict is a separate call so it can
# see Python's maths instead of guessing it.
EXTRACT_PROMPT = """You read Indian salary slips (payslips) for an NBFC's fraud check. The PDF may hold one or several months.
0. First say what the document is (document_type). Only a monthly salary slip / payslip counts as PAYSLIP — a bank statement, Form 16, offer letter or salary certificate does not. If it isn't a payslip, return an empty payslips list.
1. For each payslip extract: employer, employee, employee ID, designation, UAN and PF account / member number exactly as printed (empty if not printed), pay month (YYYY-MM), every earning and deduction line for the CURRENT month (ignore YTD/annual columns) with its type — when a payslip prints both a standard / rate / fixed column and an actual / earned column, put the earned amount (the one that adds up to gross pay) in amount and the full-month figure in standard; with unpaid (LOP) days the earned amount is the smaller one — the printed gross earnings, total deductions and net pay (0 if a total isn't printed), and the net pay in words converted to a number (e.g. "Forty Five Thousand Three Hundred Fifty Only" -> 45350; 0 if not printed). Also the attendance days as printed: paid days (days worked / present / payable), total days in the month (working or calendar days) and LOP / absent days — 0 for any that isn't printed.
2. List visual signs of tampering you can actually see: amounts in a different font/size/alignment, uneven spacing, overwritten or blurred numbers, pasted or low-quality logo, misspelt company name, missing employer address. Empty list if none — never guess."""


# Default mode: Gemini only gets the redacted salary lines, not the PDF
EXTRACT_TEXT_PROMPT = """You read the text of Indian salary slips (payslips) for an NBFC's fraud check. Names and ID numbers were removed before you got it — leave those fields empty. The text may hold one or several months.
First say what the document is (document_type), using the text and the keyword scan of the whole document. Only a monthly salary slip / payslip counts as PAYSLIP — a bank statement, Form 16, offer letter or salary certificate does not. If it isn't a payslip, return an empty payslips list.
For each payslip extract: pay month (YYYY-MM), every earning and deduction line for the CURRENT month (ignore YTD/annual columns) with its type — when a payslip prints both a standard / rate / fixed column and an actual / earned column, put the earned amount (the one that adds up to gross pay) in amount and the full-month figure in standard; with unpaid (LOP) days the earned amount is the smaller one — the printed gross earnings, total deductions and net pay (0 if a total isn't printed), and the net pay in words converted to a number (0 if not printed). Also the attendance days as printed: paid days (days worked / present / payable), total days in the month (working or calendar days) and LOP / absent days — 0 for any that isn't printed. Text from two-column tables may put an earning and a deduction on the same line — split them correctly."""


def _items(kinds: list[str]) -> dict:
    return {
        "type": "array",
        "items": {
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "type": {"type": "string", "enum": kinds},
                "amount": {"type": "number", "description": "actually paid / earned this month"},
                "standard": {"type": "number", "description": "full-month standard / rate amount if printed separately, else 0"},
            },
            "required": ["name", "type", "amount", "standard"],
        },
    }


_PAYSLIP_PROPS = {
    "employer_name": {"type": "string"},
    "employee_name": {"type": "string"},
    "employee_id": {"type": "string"},
    "designation": {"type": "string"},
    "uan": {"type": "string"},
    "pf_number": {"type": "string"},
    "pay_month": {"type": "string", "description": "YYYY-MM"},
    "earnings": _items(["BASIC", "DA", "HRA", "OTHER"]),
    "gross_earnings": {"type": "number"},
    "deductions": _items(["PF", "ESI", "PT", "TDS", "OTHER"]),
    "total_deductions": {"type": "number"},
    "net_pay": {"type": "number"},
    "net_pay_in_words_value": {"type": "number"},
    "paid_days": {"type": "number"},
    "total_days": {"type": "number"},
    "lop_days": {"type": "number"},
}
_DOC_TYPE = {"type": "string", "enum": list(DOC_TYPES)}
_SLIPS = {"type": "array", "items": {"type": "object", "properties": _PAYSLIP_PROPS, "required": list(_PAYSLIP_PROPS)}}
_EXTRACT_SCHEMA = {
    "type": "object",
    "properties": {"document_type": _DOC_TYPE, "payslips": _SLIPS, "visual_red_flags": {"type": "array", "items": {"type": "string"}}},
    "required": ["document_type", "payslips", "visual_red_flags"],
}
_EXTRACT_TEXT_SCHEMA = {
    "type": "object",
    "properties": {"document_type": _DOC_TYPE, "payslips": _SLIPS},
    "required": ["document_type", "payslips"],
}
_REVIEW_SCHEMA = {
    "type": "object",
    "properties": {
        "verdict": {"type": "string", "enum": ["GENUINE", "NEEDS_REVIEW", "PROBABLY_FAKE", "FAKE"]},
        "confidence": {"type": "string", "enum": ["LOW", "MEDIUM", "HIGH"]},
        "summary": {"type": "string", "description": "2-3 sentence note for the credit manager"},
        "red_flags": {"type": "array", "items": {"type": "string"}},
        "positives": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["verdict", "confidence", "summary", "red_flags", "positives"],
}


def _post(url: str, payload: dict) -> tuple[int, bytes]:
    """POST with a hard total deadline. requests' timeout is per read, so a
    trickling connection can otherwise hang for minutes (seen on the free tier)."""
    deadline = time.monotonic() + GEMINI_DEADLINE_S
    headers = {"x-goog-api-key": GEMINI_API_KEY, "Content-Type": "application/json"}
    with requests.post(url, headers=headers, json=payload, timeout=(10, GEMINI_DEADLINE_S), stream=True) as resp:
        chunks = []
        # read1 returns whatever has arrived (iter_content waits for a full chunk),
        # so the deadline is checked even when bytes trickle in
        while chunk := resp.raw.read1(65536, decode_content=True):
            chunks.append(chunk)
            if time.monotonic() > deadline:
                raise requests.exceptions.Timeout("total deadline exceeded")
        return resp.status_code, b"".join(chunks)


def _gemini(parts: list[dict], system: str, schema: dict) -> tuple[dict, str]:
    payload = {
        "systemInstruction": {"parts": [{"text": system}]},
        "contents": [{"role": "user", "parts": parts}],
        "generationConfig": {
            "responseMimeType": "application/json",
            "responseJsonSchema": schema,
            "thinkingConfig": {"thinkingLevel": "low"},
        },
    }
    last_err = None
    for model in dict.fromkeys([GEMINI_MODEL, GEMINI_FALLBACK_MODEL]):
        try:
            status, body = _post(GEMINI_URL.format(model=model), payload)
        except (requests.exceptions.RequestException, urllib3.exceptions.HTTPError) as e:
            last_err = f"Gemini {model} unreachable ({type(e).__name__})"
            continue
        if status in (429, 500, 503):
            last_err = f"Gemini {model} busy ({status})"
            continue
        if status >= 400:
            raise RuntimeError(f"Gemini {model} error {status}: {body[:200].decode('utf-8', 'ignore')}")
        cand = (json.loads(body).get("candidates") or [{}])[0]
        text = "".join(p.get("text", "") for p in cand.get("content", {}).get("parts", []) if not p.get("thought"))
        if not text:
            raise RuntimeError(f"Gemini returned no answer ({cand.get('finishReason', 'unknown')})")
        return json.loads(text), model
    raise RuntimeError(last_err or "Gemini unavailable")


def _evidence_for_ai(m: dict, checks: list[dict]) -> dict:
    """Only tool names, dates, counts and fonts — author/title can carry the employee's name."""
    return {
        "today": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        "producer": m["producer"], "creator": m["creator"], "xmp_tools": m["xmp_tools"],
        "created": m["created"], "modified": m["modified"], "pages": m["pages"],
        "pdf_version": m["pdf_version"], "encrypted": m["encrypted"], "saves": m["saves"],
        "fonts": m["fonts"], "has_text": m["has_text"],
        "checks": [{k: c[k] for k in ("label", "status", "detail")} for c in checks],
    }


# ─── Verdict ─────────────────────────────────────────────────

_RANK = {"GENUINE": 0, "NEEDS_REVIEW": 1, "PROBABLY_FAKE": 2, "FAKE": 3}
# Salary-structure checks reflect the employer's compliance, not forgery — shown, but not counted
WEAK_CHECKS = {"basic", "hra"}


def _rule_verdict(checks: list[dict], unlocked: bool = False) -> str:
    fails = sum(c["status"] == "fail" for c in checks)
    warns = sum(c["status"] == "warn" and c["id"] not in WEAK_CHECKS for c in checks)
    if fails >= 2 or (fails == 1 and warns >= 2):
        return "FAKE"
    if fails == 1 or warns >= 2:
        return "PROBABLY_FAKE"
    # Unlocked / re-saved: the content checked out, but the file's origin can't be verified
    return "NEEDS_REVIEW" if unlocked else "GENUINE"


def check_payslip(pdf_bytes: bytes, password: str = "", filename: str = "", progress=None) -> dict:
    """progress(pct, upto, label) is called as each stage starts — the UI's progress bar
    jumps to pct and creeps towards upto while the stage runs."""
    step = progress or (lambda pct, upto, label: None)
    step(5, 9, "Reading the PDF")
    reader = _open(pdf_bytes, password)
    step(10, 20, "Checking file details — metadata, fonts, edits")
    f = pdf_forensics(pdf_bytes, reader, filename)

    # Gate 1: is this a payslip at all? Clear cases are decided locally — nothing is sent anywhere
    step(22, 28, "Confirming it's a payslip")
    # Layout text keeps the page's columns apart; plain extraction can jam a whole table into one
    # line (seen with Crystal Reports), so read from layout text and fall back to the plain text
    layout = "\n".join(f["layout_texts"])
    text = layout if layout.strip() else f["text"]
    doc = classify_document(text + "\n" + f["text"])
    if doc["verdict"] == "other":
        if doc["other_type"]:
            raise NotPayslipError(doc["other_type"], f"It looks like {_a(doc['other_type'])} (found: {', '.join(doc['other_terms'][:4])}).")
        raise NotPayslipError(None, "No earnings, deductions or net pay found in it.")

    ids = find_ids(text + "\n" + f["text"])
    # full = PDF to Gemini (paid tier); text = redacted salary lines (default); metadata = scanned PDF, nothing to read
    mode = "full" if GEMINI_SEND_PDF else "text" if f["metadata"]["has_text"] else "metadata"
    result = {
        "metadata": f["metadata"],
        "mode": mode,
        "ai": None,
        "ai_error": None if GEMINI_API_KEY else "GEMINI_API_KEY not configured — verdict from rules only.",
        "payslips": [],
        "ids": ids,
        "sent_to_ai": None,
        "unlocked": f["unlocked"],
    }

    content_checks = []
    if GEMINI_API_KEY and mode != "metadata":
        # Step 1: Gemini structures the salary figures, Python checks the maths and the payroll rules
        step(30, 64, "Reading the salary figures (AI)")
        try:
            if mode == "full":
                if reader.is_encrypted:  # Gemini can't open password-protected PDFs
                    buf = io.BytesIO()
                    PdfWriter(clone_from=reader).write(buf)
                    pdf_bytes = buf.getvalue()
                ext, _ = _gemini(
                    [{"inline_data": {"mime_type": "application/pdf", "data": base64.b64encode(pdf_bytes).decode()}},
                     {"text": "Read this payslip PDF."}],
                    EXTRACT_PROMPT, _EXTRACT_SCHEMA,
                )
            else:
                result["sent_to_ai"] = redact_for_ai(text)
                hints = (f"Keyword scan of the whole document — payslip signs: {', '.join(doc['signs']) or 'none'}; "
                         f"other-document terms: {', '.join(doc['other_terms']) or 'none'}")
                ext, _ = _gemini([{"text": hints + "\n\nPayslip text:\n" + result["sent_to_ai"]}], EXTRACT_TEXT_PROMPT, _EXTRACT_TEXT_SCHEMA)

            # Gate 2: when the keyword scan was unsure, the AI's reading of the document decides
            ai_type = ext.get("document_type")
            if doc["verdict"] != "payslip" and ai_type and ai_type != "PAYSLIP":
                if ai_type in DOC_TYPES and ai_type != "OTHER":
                    raise NotPayslipError(DOC_TYPES[ai_type], f"The AI read it as {_a(DOC_TYPES[ai_type])}.")
                raise NotPayslipError(None, "The AI couldn't find a monthly salary slip in it.")
            result["doc_ai_type"] = ai_type

            # Employer / employee / UAN / PF no. come from the local text (redacted in text mode)
            step(65, 72, "Checking PF, TDS, attendance and tax rules")
            slips = ext.get("payslips") or []
            headers = [page_header(t) for t in f["layout_texts"]]
            for i, s in enumerate(slips):
                h = headers[i] if len(headers) == len(slips) else (headers[0] if headers else {})
                for k, v in h.items():
                    if mode == "text" or not s.get(k):
                        s[k] = v
            content_checks, result["payslips"] = _payslip_checks(slips, f["created"])
            if mode == "full":
                visual = ext.get("visual_red_flags") or []
                content_checks.append(_check("visual", "Visual check", "warn" if visual else "pass",
                                             "; ".join(visual) if visual else "No visual signs of tampering."))
        except NotPayslipError:
            raise
        except Exception as e:
            result["ai_error"] = str(e)

    # The text layer is the primary source for UAN / PF no.; Gemini fills gaps (scans, odd layouts)
    slips = result["payslips"]
    if slips:
        ids["uan"] = ids["uan"] or re.sub(r"\D", "", slips[0].get("uan") or "") or None
        ids["pf_number"] = ids["pf_number"] or (slips[0].get("pf_number") or "").strip() or None
    pf_deducted = any(s["pf"] for s in slips) if slips else None
    ids["uan_valid"] = bool(ids["uan"]) and len(ids["uan"]) == 12
    id_checks = [_id_check(ids, pf_deducted)] if f["metadata"]["has_text"] or slips else []
    checks = [_doc_check(doc, result.get("doc_ai_type"))] + f["checks"] + id_checks + content_checks

    if GEMINI_API_KEY and not result["ai_error"]:
        # Step 2: verdict from all the evidence (text only)
        step(75, 95, "AI reviewing the evidence")
        try:
            system = REVIEW_PROMPT + {"full": "", "text": TEXT_PROMPT_EXTRA, "metadata": METADATA_PROMPT_EXTRA}[mode]
            evidence = _evidence_for_ai(f["metadata"], checks)
            ai, model = _gemini([{"text": "Evidence:\n" + json.dumps(evidence, indent=1)}], system, _REVIEW_SCHEMA)
            result["ai"] = {**ai, "model": model}
        except Exception as e:
            result["ai_error"] = str(e)

    # Rules set the floor: the AI can raise the verdict but never clear a failed check
    verdict = _rule_verdict(checks, bool(f["unlocked"]))
    if result["ai"] and _RANK[result["ai"]["verdict"]] > _RANK[verdict]:
        verdict = result["ai"]["verdict"]
    result["checks"] = checks
    result["verdict"] = verdict
    step(100, 100, "Done")
    return result
