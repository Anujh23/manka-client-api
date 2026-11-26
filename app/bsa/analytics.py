"""Underwriting analytics on verified transactions."""
from __future__ import annotations

import math
import re
import statistics
from collections import Counter, defaultdict
from datetime import date, timedelta

from . import banks as K
from .models import Txn
from .utils import has_any, month_key, norm

SALARY_RE = re.compile(K.SALARY_RE)
BOUNCE_RE = re.compile(K.BOUNCE_RE)
BOUNCE_SELF_RE = re.compile(K.BOUNCE_SELF_RE)
BOUNCE_CONTEXT_RE = re.compile(K.BOUNCE_CONTEXT_RE)
PRESENTMENT_RE = re.compile(K.PRESENTMENT_RE)
TRANSFER_CHANNEL_RE = re.compile(K.TRANSFER_CHANNEL_RE)
TRANSFER_RETURN_HEAD_RE = re.compile(K.TRANSFER_RETURN_RE)
DEPOSIT_RETURN_RE = re.compile(K.DEPOSIT_RETURN_RE)
LENDER_RE = re.compile(K.LENDER_RE)
PERIOD_TOKEN_RE = re.compile(K.PERIOD_TOKEN_RE)
# card detail line: '<CC><6-digit ref><CUR> <amount> <MERCHANT> A89500001' (HSBC POS / e-com rows)
CARD_MERCHANT_RE = re.compile(r"\b[A-Z]{2}\d{6}[A-Z]{3}\s+[\d,]+\.\d{2}\s+(.+?)(?:\s+A\d{8}\b|$)")
INDIAN_IMPS_IN_RE = re.compile(r"(?:^|[\s/])TRANSFER FROM \d+\s*/IMPS/P2A/[\d ]+/\s*(.+?)\s*/BRANCH\b")
# Indian Bank POS: 'MERCHNT: 27380098 5028 D MART VANASTHALI HYDERAB IN POS TXN SEQ NO ..' - merchant before 'IN POS'
INDIAN_POS_RE = re.compile(r"MERCHNT:\s*(?:\d+\s+)*(.+?)\s+IN POS TXN\b")
# Indian Bank posting-branch label printed around the fields: '/BRANCH : ATM SERVICE BRANCH ..', 'BRANCH : BARKATPURA/',
# 'BRANCH ICIC0002365/<payee>' (the label before the IFSC), 'BRANCH TRANSFER FROM <a/c>' - never the party
INDIAN_BRANCH_RE = re.compile(r"\bBRANCH\s*:\s*(?:ATM SERVICE BRANCH\b|ATM SERV\w*$|[A-Z]+(?=\s*/))"
                              r"|\bBRANCH(?=\s+(?:[A-Z]{4}0[A-Z0-9]{6}|TRANSFER (?:FROM|TO))\b)")
# Indian Bank row wrapped over a page break: the first row of the next page starts with the previous row's tail
# ('/UPI/<rrn>/Payment from PhonePe /BRANCH : ATM SERVICE BRANCH TRANSFER FROM <a/c> NEFT/../PRAJVI VOY/').
# A narration never starts with a '/' field; the row's own text starts at the next 'TRANSFER FROM/TO' or IFSC head
INDIAN_SPILL_RE = re.compile(r"^/.*?\bBRANCH\s*:\s*[A-Z ]+?\s+(?=TRANSFER (?:FROM|TO)\b|[A-Z]{4}0[A-Z0-9]{6}/)")
# TJSB wraps a long narration around the date line's 'Txn Reference' + cheque no:
# 'UPI/DR/646449185838/Jay Prakash 42-171428188 000000 Yadav/FINO..' (the name is 'Jay Prakash Yadav')
SPLICED_REF_RE = re.compile(r"(?<=\s)\d{2,3}-\d{7,9} \d{6}(?=\s|$)")
# posting time printed before the narration: '23:05:42 Salary for April', '08:08:12 PM - Mahindra /AXI..'
TIME_RE = re.compile(r"(?<![\d:])\d{1,2}:\d{2}:\d{2}(?:\s?[AP]M\b)?(?![\d:])")
# ICICI-style 'Mode' column printed inside the narration: 'NAMAN NET BANKING FINLEASE PRIVA', 'MUTHOOT MOBILE
# BANKING FINCORP', 'EDGEVERVE SYSTEMS CMS TRANSACTION LIMITE' - a channel label, never part of a name
MODE_LABEL_RE = re.compile(r"\b(?:MOBILE BANKING|NET BANKING|CMS TRANSACTION)\b")
# branch column after the payee: DNSB 'UPI DR <rrn> SUNANDA VI CENTRAL OFFI', Equitas '..- UTIB0000553- HEAD OFFICE'
OFFICE_COL_RE = re.compile(r"\s+(?:CENTRAL|HEAD) OFFI(?:CE|C)?(?=\s*(?:[/\-|]|$))")
# page header / footer run into the last row of a page: Equitas '.. HEAD OFFICE Statement for the Period from
# 01-Jun-2026 to 14', ICICI '../Self Legends for transactions in your account statement'
PAGE_HEADER_RE = re.compile(r"\s*\b(?:STATEMENT FOR THE PERIOD FROM|LEGENDS FOR TRANSACTIONS IN YOUR ACCOUNT)\b.*$")
# SBI debit card: the 12-digit ref is glued to the merchant ('OTHPG 506505376998Phonepe Pvt Ltd 560103-')
SBI_CARD_REF_RE = re.compile(r"(?<=\bOTHPG )(\d{12})(?=[A-Z])")
# in-house payroll line '<ref> HDFC BANK SALARY FOR JUL 2026': the payer is the words right before 'SALARY FOR <month>'
PAYER_BEFORE_SALARY_RE = re.compile(r"(?:^|[\s:/])([A-Z][A-Z&.]+(?: [A-Z][A-Z&.]+){0,5}) (?:SALARY|SAL) (?:FOR|OF) "
                                    + K._MONTHS_RE + r"\b")
# ICICI inward IMPS 'MMT/IMPS/<rrn>/<remark>/<remitter> to <beneficiary>' ('Solomon Ca to SIDDHARTHA')
MMT_IMPS_RE = re.compile(r"^(?:- )?MMT ?/ ?IMPS ?/")
REMITTER_TO_RE = re.compile(r"(.*[A-Z]{2}.*?) TO (?!\d)([A-Z][A-Z ]*)")
CHANNEL_TOKENS = {"UPI", "CR", "DR", "MOB", "P2A", "P2M", "NEFT", "IMPS", "RTGS", "INB", "MB", "NA", "IB", "BY",
                  "TO", "TRANSFER", "TRF", "FROM", "TFR", "DEP", "WDL", "INF", "INFT", "ACH", "NACH", "ECS", "PAYMENT",
                  "SENT", "USING", "PAID", "VIA", "RECEIVED", "REF", "NO", "UTR", "MMT", "RECD", "INW", "OUTW", "OUT",
                  "IN", "PAY", "TXN", "BANK", "BIL", "BILLPAY", "ONL", "POS", "PCD", "VPS", "IPS", "ECOM", "DEBIT",
                  "CREDIT", "CHQ", "CLG", "D", "C", "RTN", "INWARD", "OUTWARD", "SELF", "OTHERS", "OTHER", "REV", "LP",
                  "HIB", "OTHPG"}  # SBI debit card 'by debit card-OTHPG <ref>RAZ*Airtel Money Gurgaon'


def find_lender(u: str) -> str | None:
    m = LENDER_RE.search(u)
    return m.group(0) if m else None


def _is_ref_code(w: str) -> bool:
    """'BOM621PBDHIB', 'AXLC936A1F5': digits with letters after them = reference code. 'DEXTERSAM720' is a handle."""
    # keep brand-led ids ('PAYTMQRJ8V1T3CG', 'BHARATPE9K0Q7N2', 'OIDMPOKKET..'): 5+ letters before the first digit
    if re.fullmatch(r"[A-Z]{4}[A-Z]\d{3,}", w) and w[:4] in K.IFSC_BANKS:  # NEFT UTR fragment 'IDIBH121'
        return True
    return (bool(re.search(r"\d[A-Z]", w)) and sum(ch.isdigit() for ch in w) >= 3
            and not re.match(r"[A-Z]{5,}", w))


def _dedupe_repeat(words: list[str]) -> list[str]:
    """'EXCELIUM TECHNOLOGIE EXCELIUM TECHNOLOGIE' / 'EQUITAS EQUITAS' -> first copy (fields printed twice)."""
    for k in range(1, len(words) // 2 + 1):
        tail = words[k:]
        if not tail or len(tail) > k:
            continue
        *same, (a, b) = list(zip(words, tail))
        # every word repeats exactly; only the last may be cut short (fixed-width field), and not to an initial
        if all(x == y for x, y in same) and (a == b or (len(b) >= 4 and a.startswith(b))):
            return words[:k]
    return words


# ------------------------------------------------------------------ narration helpers
# channel code glued to a bank/handle code: 'IMPSXICICIXB', 'IMPSXI', 'IMPSPAYMENT' (no party name in it)
GLUED_CHANNEL_RE = re.compile(r"(?:IMPS|NEFT|RTGS)[A-Z]+|UPI(?:AR|AB)")  # Union Bank 'UPIAR/..' (out), 'UPIAB/..' (in)
# TJSB internet-banking codes 'IBNEFT/..' / 'IBTJSB/..', 'P2A' glued to a code ('P2AMOB', PSB 'P2Al/<a/c>'),
# SBM 'ACH_DR/<payee>'
GLUED_CODE_RE = re.compile(r"IB(?:NEFT|IMPS|RTGS|[A-Z]{4})|P2[AM][A-Z]{1,3}|N?ACH_(?:DR|CR)")


def _is_glued_code(w: str) -> bool:
    return bool(GLUED_CODE_RE.fullmatch(w)) and (not w.startswith("IB") or w[2:] in ("NEFT", "IMPS", "RTGS")
                                                 or w[2:] in K.IFSC_BANKS)
_REMARK_FILLER = {"SALARY", "SALARIES", "SAL", "PAYROLL", "FOR", "OF", "THE", "MONTH", "JAN", "FEB", "MAR", "APR",
                  "MAY", "JUN", "JUL", "AUG", "SEP", "SEPT", "OCT", "NOV", "DEC", "JANUARY", "FEBRUARY", "MARCH",
                  "APRIL", "JUNE", "JULY", "AUGUST", "SEPTEMBER", "OCTOBER", "NOVEMBER", "DECEMBER"}


def _drop_pay_label(words: list[str]) -> list[str]:
    """Pay label inside a payer's name: 'TATA ELXSI SALARY DAT PU' -> 'TATA ELXSI DAT PU', 'PENINSULA SALARY FOR
    MAY' -> 'PENINSULA', 'SALARY FEB BLUE YONDER' -> 'BLUE YONDER'. 'SALARY BOLT' (an app) has no period label."""
    for i, w in enumerate(words):
        if w not in ("SALARY", "SALARIES"):
            continue
        j = i + 1
        while j < len(words) and (words[j] in _REMARK_FILLER or PERIOD_TOKEN_RE.match(words[j]) or words[j].isdigit()):
            j += 1
        if any(len(x) >= 3 and x not in _REMARK_FILLER and not x.isdigit() for x in words[:i]):
            return words[:i] + words[j:]
        # label first: only with a month right after it and a 2+ word name after the label
        if i == 0 and j > 1 and words[1] in _MONTH_WORDS and len([x for x in words[j:] if len(x) >= 3]) >= 2:
            return words[j:]
        return words
    return words


def _is_salary_remark(cand: str) -> bool:
    """'KV SEP 222 SALARY': a payer's remark (salary word + month/serial/initials), not the payer's name."""
    if not SALARY_RE.search(cand):
        return False
    rest = [w for w in cand.split() if w not in _REMARK_FILLER and not w.isdigit() and not PERIOD_TOKEN_RE.match(w)]
    return sum(len(w) for w in rest if len(w) >= 3) == 0


TJSB_POSTING_RE = re.compile(r"(?<![A-Z])(?:FNDDR|FNDCR|CSHDEP|CSHWDL)(?: \d{3})?(?![A-Z])")


def counterparty(narr: str) -> str:
    u = norm(SPLICED_REF_RE.sub("", SBI_CARD_REF_RE.sub(r"\1 ", PAGE_HEADER_RE.sub("", norm(narr)))))
    u = INDIAN_SPILL_RE.sub("", u)
    u = norm(MODE_LABEL_RE.sub(" ", OFFICE_COL_RE.sub("", TIME_RE.sub(" / ", u))))  # the time is its own field
    u = norm(TJSB_POSTING_RE.sub(" / ", u))  # TJSB 'FNDDR 051 DIPTI ..': posting code + branch code, not a name
    m = CARD_MERCHANT_RE.search(u)
    if m and sum(ch.isalpha() for ch in m.group(1)) >= 3:
        return m.group(1).strip()[:40]
    m = PAYER_BEFORE_SALARY_RE.search(u)
    if m and sum(ch.isalpha() for ch in m.group(1)) >= 3 and m.group(1).split()[0] not in CHANNEL_TOKENS:
        return m.group(1).strip()[:40]
    m = INDIAN_IMPS_IN_RE.search(u)
    if m:
        # Indian Bank inward IMPS: '/IMPS/P2A/<rrn>/ [/]<remarks>/<remitter>/BRANCH' - the remitter is the last field
        # ('/IMPSXICICIXB/SOLOMON C', '/LoanDisbursa/NAMAN FIN'); remarks come first and are not a name
        segs = [s.replace(".", " ") for s in m.group(1).split("/") if s.strip(" .-")]
        if len(segs) >= 2:
            cp = counterparty(segs[-1])
            if cp:
                return cp
    m = INDIAN_POS_RE.search(u)
    if m:
        cp = counterparty(m.group(1))
        if cp:
            return cp
    u = norm(INDIAN_BRANCH_RE.sub(" ", u))
    parts = re.split(r"[/\-:*|]+|\s{2,}", u)
    mmt = bool(MMT_IMPS_RE.match(u))
    remark = ""
    for p in parts:
        p = p.strip(" .,")
        if not p:
            continue
        if re.fullmatch(r"[A-Z]{4}0[A-Z0-9]{6}", p):  # IFSC
            continue
        # '<remitter> to <beneficiary>' ('M S MOBILE BANKING LENDEN to NAGARAJ HE'): the remitter, not the holder
        m = REMITTER_TO_RE.fullmatch(p) if mmt else None
        if m and counterparty(m.group(1)):
            p = m.group(1)
        words = [w for w in p.split() if w not in CHANNEL_TOKENS and "@" not in w and not re.search(r"\d{4,}", w)
                 and not PERIOD_TOKEN_RE.match(w) and not _is_ref_code(w) and not GLUED_CHANNEL_RE.fullmatch(w)
                 and not _is_glued_code(w)]
        words = _drop_pay_label(_dedupe_repeat(words))
        if not words:
            continue
        cand = " ".join(words)
        letters = sum(ch.isalpha() for ch in cand)
        if letters >= 3 and letters / max(1, len(cand)) > 0.6 and not re.fullmatch(r"[A-Z]{1,4}\d*", cand):
            if _is_salary_remark(cand):  # ICICI 'MMT/IMPS/<ref>/<remark>/<payer>/<bank>': prefer the payer
                remark = remark or cand[:40]
                continue
            return cand[:40]
    return remark


def _ckey(name: str) -> str:
    return re.sub(r"[^A-Z]", "", name)[:14]


def categorize(t: Txn, salary_ok: bool = True) -> tuple[str, list[str]]:
    u = f" {norm(t.narration)} "
    tags = _bounce_tags(t, u)
    lender = find_lender(u)
    if lender:
        tags.append("lender")
    if K.GAMBLING_RE.search(u):
        tags.append("gambling")
    if has_any(u, K.CRYPTO_WORDS):
        tags.append("crypto")
    if t.direction == "CR":
        if "bounce" in tags:
            return "Bounce / Reversal", tags
        # an outward transfer coming back ('NEFT RETURN--<payee>', 'IMPS REJECT|..|UNABLE TO PROCESS') is not income
        if "transfer_return" in tags or TRANSFER_RETURN_RE.search(u) or K.FAILED_TRANSFER_RE.search(u):
            return "Refund / Reversal", tags
        if salary_ok and _has_salary_kw(u):
            return "Salary", tags
        if K.CASH_DEP_RE.search(u):
            return "Cash Deposit", tags
        if has_any(u, K.INTEREST_WORDS):
            return "Interest", tags
        if has_any(u, K.REVERSAL_WORDS):
            return "Refund / Reversal", tags
        if lender and (has_any(u, K.DISBURSAL_WORDS) or "LOAN" in u or t.amount >= 2000):
            return "Loan Disbursal", tags
        if K.UPI_RE.search(u):
            return "UPI In", tags
        if "NEFT" in u or "RTGS" in u:
            return "NEFT/RTGS In", tags
        if "IMPS" in u:
            return "IMPS In", tags
        return "Other Credit", tags
    # debits
    if "deposit_return" in tags:
        return "Deposit Return", tags
    if "bounce_charge" in tags or "bounce" in tags:
        return "Bounce Charges" if "bounce_charge" in tags else "Bounce", tags
    if has_any(u, K.CREDIT_CARD_WORDS):
        return "Credit Card Payment", tags
    if lender or has_any(u, K.LOAN_WORDS):
        return "Loan / EMI", tags
    if " ELECTRO " in u:  # HSBC debit-card purchase ('CASH W/D ... ELECTRO', 'ATM ... ELECTRO')
        return "Card Spend", tags
    # a small row naming a fee is the fee, not the cash ('FEE-ATM AOR24..' 24.78, 'ATM ANNUAL CHRG' 236)
    if K.CASH_WDL_RE.search(u) and not ((has_any(u, (" CGST", " SGST", " IGST", "MARKUP")) or K.CHARGE_RE.search(u))
                                        and t.amount < 5000):
        return "Cash Withdrawal", tags
    if "gambling" in tags:
        return "Gaming / Betting", tags
    if "crypto" in tags:
        return "Crypto", tags
    if has_any(u, K.INSURANCE_WORDS):
        return "Insurance", tags
    if K.INVESTMENT_RE.search(u):
        return "Investment", tags
    if K.CHARGE_RE.search(u) and t.amount < 5000:
        return "Bank Charges", tags
    if has_any(u, K.MANDATE_WORDS):
        return "Mandate / Auto-debit", tags
    if K.UTILITY_RE.search(u):
        return "Bills & Recharge", tags
    if K.RENT_RE.search(u):
        return "Rent", tags
    if K.POS_RE.search(u) or "ECOM" in u or "DEBIT CARD" in u or " VPS" in u or "PCD" in u:
        return "Card Spend", tags
    if K.UPI_RE.search(u):
        return "UPI Out", tags
    if "NEFT" in u or "RTGS" in u or "IMPS" in u:
        return "Transfer Out", tags
    return "Other Debit", tags


def _is_self(cp: str, holder_tokens: set[str]) -> bool:
    if not cp or not holder_tokens:
        return False
    toks = {w for w in re.findall(r"[A-Z]{3,}", cp)}
    return len(toks & holder_tokens) >= min(2, len(holder_tokens))


# ------------------------------------------------------------------ bounces
# return charges whose narration itself names the returned / failed mandate, without the shared return words:
# RBL 'SCHRG RCRVRY/ACH1RTCH050525 RATN7023003252000353 SCU' 500 + 'GST RCRVRY/ACH1RTCH050525 ..' 90 (ACH return
# no. 1 charged, return date 05-05-25, UMRN RATN7023003252000353), SBI 'CMP Failed mandate c--' 295 (250 + GST).
# Charge rows only: a debit of at most a charge's size; a normal mandate debit never carries these words.
ACH_RTCH_RE = re.compile(r"(?<![A-Z])ACH(\d?)RTCH(\d{6})(?!\d)\s?(?:[A-Z]{4})?([\d ]{0,24})")
FAILED_MANDATE_RE = re.compile(r"(?<![A-Z])FAILED MANDATE(?![A-Z])")
# Airtel Payments Bank prints no return row for a failed eNACH presentation, only its charge: 'AMOUNT DEBITED FOR
# ENACH P ENALTY AIRP702230726200214 7' (the column wrap splits PENALTY and the UMRN). A mandate word + PENALTY in
# one narration is a return charge (counted only at a charge's size) and never an instalment, whatever its size
NACH_PENALTY_RE = re.compile(r"^(?=.*(?<![A-Z])E?-?NACH(?![A-Z]))(?=.*(?<![A-Z])(?:P ?ENALTY|PE ?NALTY|PEN ?ALTY"
                             r"|PENA ?LTY|PENAL ?TY|PENALT ?Y)(?![A-Z]))")
# mandate reference in that charge: 4 letters + 16 digits, a wrap space anywhere in the digits
NACH_PENALTY_UMRN_RE = re.compile(r"(?<![A-Z])[A-Z]{4}(\d[\d ]{15,30})")
# Saraswat prints the return charge glued: 'ACHRTN051026RAZORPAYSOFT W' (ACH return of 05-10-26 on the Razorpay
# mandate, 'ACHRTN<ddmmyy><payee>'), and the part it could not recover then as 'GCHRG/NACHIWRTN' (NACH inward return).
# Charge rows only (a debit of at most a charge's size), like ACH_RTCH_RE.
ACHRTN_RE = re.compile(r"(?<![A-Z])ACHRTN(\d{6})(?!\d) ?([A-Z]{0,12})")
NACH_IW_RTN_RE = re.compile(r"(?<![A-Z])NACHIWRTN(?![A-Z])")
# a UPI transaction reference (12-digit RRN): 'UPI/DR/312069262222/..', 'UPI/REV/312069262222/..', 'UPI-REV-6208..-'
UPI_RRN_RE = re.compile(r"(?<![A-Z])UPI[/-](?:(?:DR|CR|REV|RVSL)[/-])?(\d{12})(?!\d)")
# Canara prints 'Chq: <ref>' on every row; on a UPI row it is the UPI RRN ('UPI/DR/312069262222/.. Chq: 312069262222'),
# not a cheque. Canara rows can also carry the neighbour row's text (narration shift), so on a row holding a UPI RRN
# a 12-digit label (RRN length) is dropped before the instrument-word tests (presentment / return context); a real
# cheque number ('Chq: 456789') and other words still count.
CHQ_LABEL_RE = re.compile(r"(?<![A-Z])CHQ: ?\d{12}(?!\d)")
# a debit naming a mandate / EMI is a presentation even when it carries a UPI RRN (UPI AutoPay, e-mandate): its
# 'UPI/REV/<rrn>' is a returned instalment, so the UPI-reversal veto below does not apply to it
MANDATE_DEBIT_RE = re.compile(r"MANDATE|AUTOPAY|AUTO PAY|(?<![A-Z])(?:E-?NACH|NACH|ACH|ECS|EMI)(?![A-Z])")


def _drop_upi_chq_label(u: str) -> str:
    return CHQ_LABEL_RE.sub(" ", u) if UPI_RRN_RE.search(u) else u


def _upi_rrns(*texts: str) -> set[str]:
    """UPI RRNs in the narrations, in _refs form (leading zeros dropped)."""
    return {r.lstrip("0") for u in texts for r in UPI_RRN_RE.findall(u)}


def _std_charge(x: float) -> bool:
    """A standard GST-inclusive return charge: a multiple of 50 + 18% GST (59 / 118 / 177 / 295 / 413 / 590 ..)."""
    return x >= 59 and abs(x / 1.18 - round(x / 1.18 / 50) * 50) < 0.006


def _bounce_tags(t: Txn, u: str) -> list[str]:
    """Row-level tags: 'bounce_charge' (return charge / its GST), 'bounce' (a return narration on the presented item
    or on its reversal), 'deposit_return' (a cheque the holder deposited came back unpaid), 'transfer_return' (an
    outward IMPS/NEFT/UPI coming back - not a bounce, not income)."""
    if TRANSFER_RETURN_HEAD_RE.match(u):
        return ["transfer_return"] if t.direction == "CR" else []
    if TRANSFER_CHANNEL_RE.match(u):  # 'UPI-..-RETURN VASU EMI': the sender's remark
        return []
    if DEPOSIT_RETURN_RE.search(u):
        return ["deposit_return"] if t.direction == "DR" else []
    if ACH_RTCH_RE.search(u) or FAILED_MANDATE_RE.search(u):
        return ["bounce_charge"] if t.direction == "DR" and t.amount <= 1200 else []
    if NACH_PENALTY_RE.search(u):
        return ["bounce_charge"] if t.direction == "DR" and t.amount <= 1200 else []
    if ACHRTN_RE.search(u) or NACH_IW_RTN_RE.search(u):
        return ["bounce_charge"] if t.direction == "DR" and t.amount <= 1200 else []
    if re.search(K.BOM_RTD_CHAR_RE, u):
        return ["bounce_charge"] if t.direction == "DR" and t.amount <= 1200 else []
    # BoM 'RETURN CHARGES TO <a/c> TRANSFER' has no instrument word: only a standard GST-inclusive charge debit is
    # a return charge; anything else falls through to the general rule (instrument word needed there)
    if (re.match(K.BOM_RETURN_CHARGES_RE, u) and t.direction == "DR" and t.amount <= 1200
            and _std_charge(t.amount)):
        return ["bounce_charge"]
    if not (BOUNCE_SELF_RE.search(u) or (BOUNCE_RE.search(u) and BOUNCE_CONTEXT_RE.search(_drop_upi_chq_label(u)))):
        return []
    if t.direction == "DR" and (has_any(u, K.CHARGE_WORDS) or t.amount <= 1200):
        return ["bounce_charge"]
    if t.direction == "CR" and has_any(u, K.CHARGE_WORDS):  # a return charge refunded
        return []
    return ["bounce"]


_FOOTER_RE = re.compile(r"\s*(?:HDFC BANK LIMITED|STATEMENT FOR THE PERIOD.*)$")
_CHG_STOP = {"BANK", "CHARGES", "CHARGE", "CHRG", "CHGS", "RETURN", "RETURNS", "RETN", "ECSRTN", "ECSRTNCHGS",
             "ECSRTNCHG", "NACH", "NACHDR", "DEBIT", "INCL", "ACHRET", "ACHINWDR", "CHGS", "FUNDS", "FUND", "INSUFF",
             "INSUFFICIENT", "BALANCE", "LIEN", "BOUNCECHGS", "RTNCHGS", "CHRGS", "SCREF", "ENACH", "REMARKS",
             "STATEMENT",
             "SHORTFAL", "SHORTFALL"  # PNB 'SHORTFAL REC- ACH RTN-HDFC BANK LIMITED- 10-04-2022': rest of that charge
             } | {m for m in ("JANUARY FEBRUARY MARCH APRIL JUNE JULY AUGUST SEPTEMBER OCTOBER NOVEMBER "
                                         "DECEMBER").split()}
_MON = {m: i + 1 for i, m in enumerate(("JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV",
                                        "DEC"))}


def _refs(u: str) -> set[str]:
    """Reference numbers in a narration: digit runs with 6+ significant digits."""
    return {r.lstrip("0") for r in re.findall(r"\d{6,}", u) if len(r.lstrip("0")) >= 6}


def _mk_date(y: int, m: int, d: int) -> date | None:
    try:
        return date(y if y > 100 else 2000 + y, m, d)
    except ValueError:
        return None


# ICICI ECS return code: 'ECSRTN<no>_<date><UMRN 70..>' with the date as d m yy, padded ('ECSRTN1_020926') or not
# ('ECSRTN2_4524 7012..' = return 2 on 4-5-24, 'ECSRTN30424_7010..' = 30-4-24, no return no.)
ECS_CODE_RE = re.compile(r"ECSRTN(?:(\d)_)?_?(\d{3,6})_?(70\d{14})")


def _ecs_code(u: str, posted: date):
    """(return no., return date, mandate) from an ICICI ECS return code, or None. The day and month may be printed
    without leading zeros: every valid day/month split is tried and the date closest before the posting wins."""
    m = ECS_CODE_RE.search(u)
    if not m:
        return None
    dm, yy = m[2][:-2], int(m[2][-2:])
    best = None
    for k in range(1, len(dm)):
        dd, mm = dm[:k], dm[k:]
        if len(dd) > 2 or len(mm) > 2:
            continue
        d = _mk_date(yy, int(mm), int(dd))
        if d and posted - timedelta(days=400) <= d <= posted and (best is None or d > best):
            best = d
    return (m[1] or "", best, m[3]) if best else None


def _return_date(u: str, posted: date):
    """Date of the bounced presentation printed inside a charge narration, as a date or a 'YYYY-MM' month.
    Formats seen: 'ACHRet-310326', 'RETURN CHARGES 311025', 'ECSRTNCHGS050923_SR..', 'ECSRTN1_0209267021..',
    'ECS Return on 20260602', 'RETN CHGS 08062026', ':02-08-2026', '~01DEC23~', 'EMI RTN CHARGES-NOVEMBER-2025',
    'BOUNCECHGSAPR25'. Only a date on or before the posting date and at most ~13 months older is accepted."""
    def ok(d):
        return d if d and posted - timedelta(days=400) <= d <= posted else None
    ecs = _ecs_code(u, posted)
    if ecs:
        return ecs[1]
    m = ACH_RTCH_RE.search(u)  # RBL 'ACH1RTCH050525': ddmmyy right after the code
    if m:
        return ok(_mk_date(int(m[2][4:]), int(m[2][2:4]), int(m[2][:2])))
    m = ACHRTN_RE.search(u)  # Saraswat 'ACHRTN051026RAZORPAYSOFT W': ddmmyy right after the code
    if m:
        return ok(_mk_date(int(m[1][4:]), int(m[1][2:4]), int(m[1][:2])))
    cands = []
    for m in re.finditer(r"(?<!\d)(\d{2})-(\d{2})-(\d{4}|\d{2})(?!\d)", u):  # Kotak 'RTNCHG-189500/04-04-24'
        cands.append(_mk_date(int(m[3]), int(m[2]), int(m[1])))
    for m in re.finditer(r"(?<!\d)(\d{8})(?!\d)", u):
        s = m[1]
        cands.append(_mk_date(int(s[:4]), int(s[4:6]), int(s[6:])) if s.startswith("20") else None)
        cands.append(_mk_date(int(s[4:]), int(s[2:4]), int(s[:2])))
    m = re.search(r"ECSRTN\d?_(\d{6})", u)
    if m:
        cands.append(_mk_date(int(m[1][4:]), int(m[1][2:4]), int(m[1][:2])))
    for m in re.finditer(r"(?<!\d)(\d{6})(?!\d)", u):
        s = m[1]
        cands.append(_mk_date(int(s[4:]), int(s[2:4]), int(s[:2])))
    for m in re.finditer(r"(?<![A-Z\d])(\d{1,2})\s?(" + "|".join(_MON) + r")(\d{2})(?!\d)", u):
        cands.append(_mk_date(int(m[3]), _MON[m[2]], int(m[1])))
    # HSBC '.. INSUFFICIENT FUNDS 3480094805 01-May-26' (charges batched a month later): tried last, so a narration
    # already dated by a format above keeps that date
    for m in re.finditer(r"(?<![A-Z\d])(\d{1,2})-(" + "|".join(_MON) + r")-(\d{4}|\d{2})(?!\d)", u):
        cands.append(_mk_date(int(m[3]), _MON[m[2]], int(m[1])))
    for d in cands:
        if ok(d):
            return d
    m = re.search(r"(?:RTN|BOUNCE)[A-Z ]*?[-\s]?(JAN|FEB|MAR|APR|MAY|JUN|JUL|AUG|SEP|OCT|NOV|DEC)[A-Z]*[-\s]?"
                  r"(?:(20\d{2})\d?|(\d{2}))(?!\d)", u)  # 'EMI RTN CHARGES-NOVEMBER-20250' (stray digit after year)
    if m:
        d = _mk_date(int(m[2] or m[3]), _MON[m[1]], 1)
        if d and d <= posted and d >= posted - timedelta(days=400):
            return f"{d.year:04d}-{d.month:02d}"
    return None


def _charge_key(u: str, posted: date):
    """What identifies one return across its charge rows (a charge is often recovered in parts on different days:
    'SR931248059' 248.78 + 0.01 + 341.19). A bank ref (SCREF.., SR..) is one return; otherwise the return date +
    the amount / RN / mandate / lender printed in the narration."""
    u = _FOOTER_RE.sub("", u)
    m = re.search(r"SCREF\d+", u)
    if m:
        return ("REF", m[0]), True
    m = re.search(r"_SR(\d+)(?: (\d{1,3})(?=\D|$))?", u)
    if m:
        return ("REF", "SR" + m[1] + (m[2] or "")), True
    m = re.search(r"NACH_AD_RTN_CHRG~[^~]*~[^~]*~([^~\s]+)", u)
    if m:
        return ("REF", "NACH" + m[1]), True
    # IDBI 'ACH-RTN-CHRG-3458718990': one ref per return; its 118 (100 + GST) and 590 (500 + GST) rows are full
    # charges of separate returns, not part recoveries of one 590
    m = re.search(r"(?<![A-Z])ACH-RTN-CHRG-(\d{8,})(?!\d)", u)
    if m:
        return ("REF", "ACHRTNCHRG" + m[1]), True
    # RBL 'ACH1RTCH050525 RATN70230032 52000353': return no. + return date + UMRN digits (the UMRN wraps at a
    # different place on the charge and its GST row, so its digits are joined)
    m = ACH_RTCH_RE.search(u)
    if m:
        umrn = re.sub(r"\D", "", m[3])
        return ("REF", "ACHRTCH" + m[1] + m[2] + (umrn[:16] if len(umrn) >= 16 else "")), True
    # Saraswat 'ACHRTN051026RAZORPAYSOFT W': return date + payee. Not one return per key: two mandates through one
    # aggregator can return the same day (2 x 295 on one code), so the group is counted by the charge unit
    m = ACHRTN_RE.search(u)
    if m:
        return ("REF", "ACHRTN" + m[1] + m[2][:8]), False
    # 'ECSRTN1_0209267021504252008330': return no. + return date + mandate (UMRN) = one return
    ecs = _ecs_code(u, posted)
    if ecs:
        return ("REF", "ECS" + ecs[0] + ecs[1].isoformat() + ecs[2]), True
    m = re.search(r"ECSRTN(\d?)_(\d{6})(\d{8,})", u)
    if m and _return_date(u, posted):
        return ("REF", "ECS" + m[1] + m[2] + m[3]), True
    m = re.search(r"ECSRTN\d*_\d*?(70\d{14})", u)
    if m:  # date garbled ('ECSRTN2_45247012707220004290'): the mandate alone
        return ("MANDATE", m[1]), True
    # Airtel eNACH penalty: one per failed presentation. The same mandate (UMRN digits joined across the wrap) in
    # the same month is one return; another mandate, or the same one in a later month, is another
    if NACH_PENALTY_RE.search(u):
        m = NACH_PENALTY_UMRN_RE.search(u)
        umrn = re.sub(r"\D", "", m[1])[:16] if m else ""
        return ("REF", "NACHPEN" + (umrn if len(umrn) == 16 else "") + "/" + month_key(posted)), True
    rd = _return_date(u, posted)
    amt = re.search(r"AMT\s?([\d.]+)", u)
    rn = re.search(r"RN-(\d+)", u)
    mandate = re.search(r"ECSRTN\d?_\d{6}(\d{8,})", u) or re.search(r"(?<!\d)(\d{9})-EMI RTN", u)
    words = [w for w in re.findall(r"[A-Z]{4,}", re.sub(r"EPR\w+|MIR\w+", " ", u)) if w not in _CHG_STOP]
    key = ("K", str(rd) if rd else None, amt[1] if amt else None, rn[1] if rn else None,
           mandate[1] if mandate else None, words[0][:8] if words else None)
    return key, False


def detect_bounces(txns: list[Txn], start: date, printed_start: date | None = None) -> dict:
    """Bounce = a mandate / EMI / cheque presented on the account and returned unpaid. Evidence, strongest first:
    1. the presentation debit followed (same day to 3 days, within 15 rows) by a credit of the same amount that
       repeats its narration or reference, or carries a return word (HDFC/IOB/JANA print a bounce this way, often
       with the balance going negative). The credit is tagged 'bounce_reversal' - it is neither income nor a loan.
    2. a return narration on the presented item ('I/W CHQ RETURN', 'ACH DR RETN BALANCE INSUFFICIENT').
    3. return charges only (Karnataka / Kotak / ICICI print no reversal): charge rows of one return are merged
       (same ref, or same return date + amount/RN/mandate/lender); a return dated before the statement starts is
       a prior-period return, reported but not counted.
    Charges that name the date of a return already seen in 1/2 confirm that event instead of adding one."""
    n = len(txns)
    paired_cr, paired_dr = set(), set()
    events = []  # bounce events: dicts with '_d' (date) for matching
    for i, t in enumerate(txns):
        if t.direction != "DR" or "bounce_charge" in t.tags or t.amount < 1:
            continue
        u = norm(t.narration)
        # 'DPI 137528114 CHQ ..' (HDFC delayed-payment interest) is reversed along with its EMI: same bounce
        if (TRANSFER_CHANNEL_RE.match(f" {u}") or u.startswith("DPI ")
                or not (PRESENTMENT_RE.search(_drop_upi_chq_label(u)) or "bounce" in t.tags)):
            continue
        refs = _refs(u)
        for j in range(i + 1, min(n, i + 16)):
            c = txns[j]
            if (c.date - t.date).days > 3:
                break
            if c.direction != "CR" or j in paired_cr or abs(c.amount - t.amount) > 0.005:
                continue
            cu = norm(c.narration)
            shared = refs & _refs(cu)
            # a debit and a credit sharing a UPI RRN are a UPI reversal / refund ('UPI/REV/<rrn>', a merchant refund
            # quoting the payment's RRN), not a returned presentation (Canara: hex txn ids shared too) - unless the
            # debit names a mandate / EMI
            if shared & _upi_rrns(u, cu) and not MANDATE_DEBIT_RE.search(u):
                continue
            if cu == u or shared or "bounce" in c.tags:
                paired_cr.add(j)
                paired_dr.add(i)
                events.append({"_d": t.date, "_i": i, "_j": j, "source": "reversal_pair"})
                break
    # 2. return narrations not proven by a pair
    for i, t in enumerate(txns):
        if "bounce" not in t.tags or i in paired_cr or i in paired_dr:
            continue
        j = None
        if t.direction == "CR":  # the reversal: find the presented debit just before it
            for k in range(i - 1, max(-1, i - 16), -1):
                p = txns[k]
                if (t.date - p.date).days > 3:
                    break
                if p.direction == "DR" and k not in paired_dr and abs(p.amount - t.amount) <= 0.005:
                    j = k
                    break
            if j is not None:
                paired_dr.add(j)
                paired_cr.add(i)
                events.append({"_d": txns[j].date, "_i": j, "_j": i, "source": "return_narration"})
                continue
            paired_cr.add(i)
            events.append({"_d": t.date, "_i": None, "_j": i, "source": "return_narration"})
        else:
            paired_dr.add(i)
            events.append({"_d": t.date, "_i": i, "_j": None, "source": "return_narration"})
    for i in paired_dr:
        if "bounced_presentment" not in txns[i].tags:
            txns[i].tags.append("bounced_presentment")
    for j in paired_cr:
        c = txns[j]
        for tag in ("bounce", "bounce_reversal"):
            if tag not in c.tags:
                c.tags.append(tag)
        c.category = "Bounce / Reversal"

    # 3. charges, merged per return
    # a bare 'GST' row right after a return charge, same day, exactly 18% of it, is that charge's GST (BoM 'ECS DR
    # RTD CHAR' 300.00 then 'GST' 54.00): tag it so it rides on its charge below
    for i in range(1, n):
        t, p = txns[i], txns[i - 1]
        if ("bounce_charge" in p.tags and "bounce_charge" not in t.tags and t.direction == "DR"
                and p.direction == "DR" and t.date == p.date and re.match(r"GST\b", norm(t.narration))
                and abs(t.amount - round(p.amount * 0.18, 2)) <= 0.011):
            t.tags.append("bounce_charge")
            t.category = "Bounce Charges"
    # per-presentation references (9+ digits) of the returns proven above: an undated charge row repeating one within
    # a week (KVB 'INWARD CHEQUE RETURN SB :003052593393' after 'TO CLG:ECS .. 003052593393' / 'By Clg(Rej):ECS ..
    # 003052593393') is that return's charge - it confirms the return. A number that recurs on other rows (loan no.,
    # UMRN, the holder's a/c no.) is no such ref: every non-charge row holding it must be this return (same amount,
    # within 3 days)
    ref_rows: dict = defaultdict(list)
    for t in txns:
        if "bounce_charge" not in t.tags:
            for r in {x for x in _refs(norm(t.narration)) if len(x) >= 9}:
                ref_rows[r].append(t)
    ev_refs = []
    for ei, e in enumerate(events):
        x0 = txns[e["_i"] if e["_i"] is not None else e["_j"]]
        own = {r for x in (e["_i"], e["_j"]) if x is not None for r in _refs(norm(txns[x].narration)) if len(r) >= 9}
        ev_refs.append((ei, e["_d"], {r for r in own if all(abs(t.amount - x0.amount) <= 0.005
                                                             and abs((t.date - x0.date).days) <= 3
                                                             for t in ref_rows[r])}))
    groups: dict = {}
    order = []
    last_key_by_day: dict = {}
    last_row_by_day: dict = {}
    riders = []  # (GST row, the charge row it rides on)
    for t in txns:
        if "bounce_charge" not in t.tags:
            continue
        u = norm(t.narration)
        if re.match(r"GST\b", u) and t.date in last_key_by_day:  # 'GST FOR NACHDR RETURN:..' rides on its charge
            key, is_ref = last_key_by_day[t.date]
            riders.append((t, last_row_by_day[t.date]))
        else:
            last_row_by_day[t.date] = t
            key, is_ref = _charge_key(u, t.date)
            if key[0] == "MANDATE":  # undated row of a mandate whose return is already seen
                key = next((k for k in reversed(order) if k[0] == "REF" and k[1].startswith("ECS")
                            and k[1].endswith(key[1])), key)
            elif key[0] == "K" and (key[1] is None or len(key[1]) == 10):  # undated, or dated to the day
                tr = _refs(u)
                hit = [(t.date - d, ei) for ei, d, rs in ev_refs if rs & tr and 0 <= (t.date - d).days <= 7]
                if hit:  # counted by the charge unit: a second full charge is a re-presentation returned again
                    key, is_ref = ("PAIR", min(hit)[1]), False
        last_key_by_day[t.date] = (key, is_ref)
        if key not in groups:
            groups[key] = {"rows": [], "ref": is_ref}
            order.append(key)
        groups[key]["rows"].append(t)
    # a charge recovered in part when the balance ran out (its last row leaves the balance at 0.00) and the rest
    # later (KVB 372.54 then 40.46 = 413; Saraswat 'ACHRTN051026..' 7.05 then 'GCHRG/NACHIWRTN' 287.95 = 295): a
    # later charge row printing no date / amount / RN / mandate / ref, itself not a standard charge, that tops that
    # group up to exactly the full charge of another return in this statement, or to a standard GST-inclusive
    # charge, is the rest of that charge, not another return (oldest due first)
    pos = {id(t): i for i, t in enumerate(txns)}
    totals = {k: round(sum(t.amount for t in g["rows"]), 2) for k, g in groups.items()}
    topped = set()
    for ck in [k for k in order if k[0] == "K" and not any(k[1:5])]:
        for t in list(groups[ck]["rows"]):
            if _std_charge(t.amount):
                continue
            for sk in order:
                if sk == ck or sk in topped or not groups.get(sk) or sk[0] == "K" and not any(sk[1:5]):
                    continue
                last = groups[sk]["rows"][-1]
                if (last.balance is None or abs(last.balance) > 0.005 or pos[id(last)] > pos[id(t)]
                        or not 0 <= (t.date - last.date).days <= 31):
                    continue
                s = round(totals[sk] + t.amount, 2)
                if _std_charge(s) or any(abs(s - totals[fk]) <= 0.005 for fk in order if fk not in (sk, ck)):
                    groups[ck]["rows"].remove(t)
                    groups[sk]["rows"].append(t)
                    totals[sk] = s
                    topped.add(sk)
                    break
    order = [k for k in order if groups[k]["rows"]]
    # the bank's full return charge (590 / 649 / 354): the largest amount charged more than once; partial
    # recoveries (0.01, 22.38 ..) repeat too, so the most frequent amount is not it. No row may exceed it.
    # A GST row riding on its charge is part of that charge (BoM 300 + 54 = 354, the same unit as its 354 rows)
    def _unit(gs):
        full = {id(t): t.amount for g in gs for t in g["rows"]}
        for rt, base in riders:
            if id(rt) in full and id(base) in full:
                full[id(base)] += full.pop(id(rt))
        amts = Counter(round(a, 2) for a in full.values())
        u_ = max((a for a, c in amts.items() if c >= 2), default=None)
        return None if u_ is not None and u_ < max(amts) else u_
    # REF groups count 1 each: learn the unit from the groups it is applied to, all groups only as a fallback
    unit = _unit([g for g in groups.values() if not g["ref"] and g["rows"]]) or (_unit(list(groups.values()))
                                                                                if groups else None)
    charge_events, prior = [], 0
    # a return dated by its month only is in the statement from the month the statement starts. The bank's printed
    # period starting on the 1st, with only posting-lag rows 1-3 days before it (ICICI 31-12 card / UPI rows in a
    # 01-01 statement), starts in the printed month: a 'RTNCHGSDec23' charge there is a prior-period return
    m_start = start
    if printed_start and printed_start.day == 1 and 0 < (printed_start - start).days <= 3:
        m_start = printed_start
    for key in order:
        g = groups[key]
        rows = g["rows"]
        total = round(sum(t.amount for t in rows), 2)
        k = 1 if (g["ref"] or not unit) else max(1, math.ceil(total / unit - 0.02))
        rd = next((d for d in (_return_date(_FOOTER_RE.sub("", norm(t.narration)), t.date) for t in rows) if d),
                  None)
        if key[0] == "PAIR" and rd is None:
            rd = events[key[1]]["_d"]
        if isinstance(rd, date):
            in_period = rd >= start
        elif isinstance(rd, str):
            in_period = rd >= month_key(m_start)
        else:
            in_period = True  # no date printed: the charge is in the statement, the return cannot be dated earlier
        # 'GCHRG/NACHIWRTN' at an odd amount that tops up no charge in this statement is the rest of a charge
        # from before it: a prior-period return, never a new one
        if (key[0] == "K" and not any(key[1:5])
                and all(NACH_IW_RTN_RE.search(norm(t.narration)) and not _std_charge(t.amount) for t in rows)):
            in_period = False
        amt = re.search(r"AMT\s?([\d.]+)", norm(rows[0].narration))
        charge_events.append({"rows": rows, "n": k, "rd": rd, "in_period": in_period, "total": total,
                              "presented": float(amt[1]) if amt else None,
                              "pair": key[1] if key[0] == "PAIR" else None})
    # a charge naming the date of a return already proven by a pair confirms it; else it is a return of its own
    claimed = set()
    for ce in charge_events:
        need = ce["n"]
        if ce["pair"] is not None:  # repeats that return's reference: confirms it; extra full charges = more returns
            claimed.add(ce["pair"])
            need -= 1
            if not need:
                continue
        if isinstance(ce["rd"], date):
            for ei, e in enumerate(events):
                if (need and ei not in claimed and e["source"] != "charge_only"
                        and abs((e["_d"] - ce["rd"]).days) <= 1):
                    claimed.add(ei)
                    need -= 1
        if not need:
            continue
        if not ce["in_period"]:
            prior += need
            continue
        if isinstance(ce["rd"], date):
            ds = [ce["rd"]] * need
        elif isinstance(ce["rd"], str):  # only the month printed ('BOUNCECHGSMAY25'): the return falls in that month
            y, mth = map(int, ce["rd"].split("-"))
            ds = [date(y, mth, 1)] * need
        else:
            # no return date printed: one undated key can span months (SBM 'NACH RTN ACH DR RAZORPAYSOFTWAREPRIV'
            # charged Apr / Jun / Aug). Each full charge row is one return, dated by its own row
            full = sorted(ce["rows"], key=lambda t: (-t.amount, t.date))[:need]
            ds = sorted(t.date for t in full)
            ds += [ce["rows"][0].date] * (need - len(ds))
        for d in ds:
            events.append({"_d": d, "_i": None, "_j": None, "source": "charge_only", "_ce": ce})

    out_events = []
    for e in events:
        if e["source"] == "charge_only":
            r = e["_ce"]["rows"][0]
            amt = -e["_ce"]["presented"] if e["_ce"]["presented"] else None
            out_events.append({"date": e["_d"].isoformat(), "narration": r.narration[:110], "amount": amt,
                               "type": "bounce", "source": "charge_only"})
            continue
        t = txns[e["_i"]] if e["_i"] is not None else txns[e["_j"]]
        out_events.append({"date": e["_d"].isoformat(), "narration": t.narration[:110], "amount": -t.amount,
                           "type": "bounce", "source": e["source"],
                           "reversed_on": txns[e["_j"]].date.isoformat() if e["_j"] is not None else None})
    for ce in charge_events:
        r = ce["rows"][0]
        rd = ce["rd"]
        out_events.append({"date": r.date.isoformat(), "narration": r.narration[:110], "amount": -ce["total"],
                           "type": "charge", "rows": len(ce["rows"]), "returns": ce["n"],
                           "return_date": rd.isoformat() if isinstance(rd, date) else rd,
                           "in_period": ce["in_period"]})
    out_events.sort(key=lambda e: (e["date"], e["type"]))
    deposit = [{"date": t.date.isoformat(), "narration": t.narration[:110], "amount": -t.amount}
               for t in txns if "deposit_return" in t.tags]
    count = sum(1 for e in out_events if e["type"] == "bounce")
    return {"count": count, "charges_count": sum(ce["n"] for ce in charge_events),
            "charge_rows": sum(len(ce["rows"]) for ce in charge_events),
            "charges_amount": round(sum(ce["total"] for ce in charge_events), 2),
            "prior_period_returns": prior, "events": out_events,
            "months_with_bounce": sorted({e["date"][:7] for e in out_events if e["type"] == "bounce"}),
            "deposit_returns_count": len(deposit), "deposit_returns": deposit}


# ------------------------------------------------------------------ self transfers (tag used only by the averages)
# words that say nothing about who the party is: titles and legal suffixes. A holder 'NAMAN COMMODITIES PVT LTD'
# must not make 'CASHFREE PAYMENTS PVT LTD ESCROW' (its payment gateway) a self transfer.
_SELF_NEUTRAL = {"MR", "MRS", "MISS", "MS", "SHRI", "SMT", "KUMARI", "DR", "PVT", "LTD", "PRIVATE", "LIMITED", "LLP",
                 "SELF"}
# a transfer remark 'self' on an account-to-account transfer: 'IMPS/6090..-SELF- DNSB0000046', 'MOB/RRN-../SELF/IMPS',
# 'IMPS:PAY:../MB Velpul/UTIB/XX2547/self', '..9738-SELF TRANSFER', 'INF/INFT/../SELF-XXX932'. Cash rows also say
# SELF ('CHQ PAID-SELF', 'CASH DEP-SELF', 'ATM/CASH WITHDRAWAL/../SELF', 'CSHWDL 051 SELF') and are not transfers;
# 'PAY LATER EXPEN/SELF' is a pay-later repayment.
SELF_REMARK_RE = re.compile(r"(?:^|[/\-\s])SELF(?=[/\-\s]|$)")
SELF_CHANNEL_RE = re.compile(r"(?<![A-Z])(?:IMPS|NEFT|RTGS|INFT|INF|UPI|MB|MOB|TRANSFER|TRF)(?![A-Z])")
SELF_NOT_TRANSFER_RE = re.compile(r"ATM|CASH|CSHWDL|CHQ|CHEQUE|PAY LATER|WDL|SWITCH")
MASKED_ACCT_RE = re.compile(r"\bX{2,}\d{4,}\b")


def _holder_self_tokens(holder_tokens: set[str]) -> set[str]:
    return {w for w in holder_tokens if w not in _SELF_NEUTRAL}


def _self_by_name(cp: str, hs: set[str]) -> bool:
    """Counterparty is the account holder. Every name word must belong to the holder (truncations such as 'KUMA',
    'VATTIP ALLY', 'KANDIBAN DA' allowed) unless the full holder name is printed ('NAMAN COMMODITIES PVT LTD
    OPERATIONS'). A relative shares the surname but adds a name of their own ('VELPULA KANAKA RAJ' for holder
    'DAVID RAJ VELPULA', 'LATA VILAS ZAMBARE' for 'PRASHANT VILAS ZAMBARE') - not self. A one-word holder name needs
    a counterparty with nothing else in it ('SANKAR DRAWDOWN CASA' is a loan recovery, not holder 'SANKAR D')."""
    if not cp or not hs:
        return False
    toks = re.findall(r"[A-Z]{3,}", cp)
    hit = {h for h in hs if any(w == h or (len(w) >= 4 and h.startswith(w)) for w in toks)}
    # a name cut by a line break / spliced ref: 'NE ERAJ YADAV', 'N EERAJ YADAV' for holder 'NEERAJ YADAV' (PSB_001)
    frags = re.findall(r"[A-Z]+", cp)
    hit |= {h for h in hs if h in {a + b for a, b in zip(frags, frags[1:])}}
    if len(hit) < min(2, len(hs)):
        return False
    extra = [w for w in toks if w not in _SELF_NEUTRAL and not any(h.startswith(w) or h.endswith(w) for h in hs)]
    return not extra or (len(hs) >= 2 and hit == hs)


# the holder's name can sit in the remark while the money goes to someone else (ICICI_001):
# 'MMT/IMPS/<rrn>/Kapil Vyas 5716/UCA FINVES/IDFC' (remark / beneficiary / bank), 'UPI/../Kapil Vyas/vrindafinlease@'
ICICI_MMT_RE = re.compile(r"MMT/IMPS/\d+/[^/]*/([^/]+)/")
LENDER_VPA_RE = re.compile(r"[A-Z0-9.\-]*(?:FIN|LEAS|LOAN|CAPITAL|CREDIT)[A-Z0-9.\-]*@")


def _paid_to_other(u: str, hs: set[str]) -> bool:
    m = ICICI_MMT_RE.search(u)
    if m and sum(ch.isalpha() for ch in m.group(1)) >= 3 and not _self_by_name(m.group(1), hs):
        return True
    return bool(LENDER_VPA_RE.search(u)) and not any(re.search(r"FIN|LEAS|LOAN|CAPITAL|CREDIT", h) for h in hs)


def _self_by_remark(u: str) -> bool:
    return bool(SELF_REMARK_RE.search(u) and SELF_CHANNEL_RE.search(u) and not SELF_NOT_TRANSFER_RE.search(u))


def _bank_words() -> set[str]:
    out = set()
    for pref, (code, _name) in K.IFSC_BANKS.items():
        out.update({pref, code})
    return out


def tag_self_transfers(txns: list[Txn], holder_tokens: set[str]) -> None:
    """Tag 'self_transfer' (used to keep own-account round trips out of the average credits AND debits):
    1. counterparty is the holder by name (_self_by_name);
    2. the narration is a transfer with a 'self' remark;
    3. the same masked account ('XX2547') as a row of 2. ('IMPS:REC:../VELPULADA/UTIB/XX2547' pairs with
       'IMPS:PAY:../MB Velpul/UTIB/XX2547/self');
    4. a beneficiary nickname = one holder name word + a bank ('SUMIT HSBC', 'SUMIT HDFC') when that bank already
       shows up on a proven self row of this statement ('NEFT|HSBCN52026.. MR SUMIT SINGH|HSBC BANK')."""
    hs = _holder_self_tokens(holder_tokens)
    banks = _bank_words()
    cash = ("Cash Withdrawal", "Cash Deposit")
    own_accts, self_banks = set(), set()
    for t in txns:
        if t.category in cash:
            continue
        u = norm(t.narration)
        by_remark = _self_by_remark(u)
        if (_self_by_name(t.counterparty, hs) and not _paid_to_other(u, hs)) or by_remark:
            t.tags.append("self_transfer")
            self_banks.update(w for w in re.findall(r"[A-Z]{3,}", u) if w in banks)
            self_banks.update(m[:4] for m in re.findall(r"\b([A-Z]{4})[0N][A-Z0-9]{6,}", u) if m[:4] in banks)
            if by_remark:
                own_accts.update(MASKED_ACCT_RE.findall(u))
    for t in txns:
        if "self_transfer" in t.tags or t.category in cash:
            continue
        u = norm(t.narration)
        if own_accts and set(MASKED_ACCT_RE.findall(u)) & own_accts:
            t.tags.append("self_transfer")
            continue
        toks = re.findall(r"[A-Z]{3,}", t.counterparty or "")
        names = [w for w in toks if w in hs]
        rest = [w for w in toks if w not in hs and w not in _SELF_NEUTRAL]
        if names and rest and all(w in banks and w in self_banks for w in rest):
            t.tags.append("self_transfer")


# money that left and came straight back: a credit for the exact amount of a debit within 3 days that carries the
# same transaction reference, and either says it is a return ('LP BOM6229T2HIB' debit / 'LP BOM6229T2HIB NONREF'
# credit, HSBC_002; 'UPI/DR/607210830209/..' / 'UPI/REV/607210830209/..', PSB_001; 'NEFT DR- ..AUBLN62026010733887455'
# / 'NEFT RETURN--..AUBLN62026010733887455', AU_002), repeats the debit narration word for word ('ACH D- BAJAJ FINANCE
# LTD-P400PSA2681313 0000003872562227' both legs, HDFC_008; 'ACH DR CASHFREE PAYMENTS IN 5497320260831816517',
# JANA_001), or shares a 14+ character reference (ACH/NEFT refs; a 10-digit phone in two UPI handles is not enough -
# 'UPI-CHAITRA R-7899907547@AXL' out and back is a payment and a repayment). Neither leg is real turnover.
_REF_TOKEN_RE = re.compile(r"[A-Z0-9]{8,}")
_IFSC_TOKEN_RE = re.compile(r"[A-Z]{4}0[A-Z0-9]{6}")
RETURN_MARK_RE = re.compile(r"(?<![A-Z])(?:REV|RVSL|REVERSAL|REVERSED|RETURN|RETN|RTN|NONREF|REJECT|REFUND)"
                            r"|\bCR FOR\b|\bCREDIT ADJUST")


def _ref_tokens(narr: str) -> set[str]:
    return {w for w in _REF_TOKEN_RE.findall(norm(narr))
            if sum(ch.isdigit() for ch in w) >= 4 and not _IFSC_TOKEN_RE.fullmatch(w)}


def reversal_pairs(txns: list[Txn]) -> set[int]:
    """ids of both legs of each debit -> same-reference same-amount credit round trip."""
    out: set[int] = set()
    open_dr: list[tuple[Txn, set[str]]] = []
    for t in sorted(txns, key=lambda x: x.date):
        refs = _ref_tokens(t.narration)
        if not refs:
            continue
        open_dr = [(d, r) for d, r in open_dr if (t.date - d.date).days <= 3]
        if t.direction == "DR":
            open_dr.append((t, refs))
            continue
        marked = bool(RETURN_MARK_RE.search(norm(t.narration)))
        for d, drefs in open_dr:
            if id(d) in out or abs(d.amount - t.amount) > 0.005:
                continue
            shared = refs & drefs
            if shared and (marked or norm(d.narration) == norm(t.narration)
                           or any(len(w) >= 14 and sum(ch.isdigit() for ch in w) >= 10 for w in shared)):
                out.update({id(d), id(t)})
                break
    return out


# ------------------------------------------------------------------ balances
def eod_series(txns: list[Txn], start: date, end: date) -> dict[date, float]:
    by_day: dict[date, float] = {}
    for t in txns:
        if t.balance is not None:
            by_day[t.date] = t.balance
    out, cur = {}, None
    first_bal = next((t.balance - t.signed for t in txns if t.balance is not None), None)
    cur = first_bal
    d = start
    while d <= end:
        if d in by_day:
            cur = by_day[d]
        if cur is not None:
            out[d] = cur
        d += timedelta(days=1)
    return out


# ------------------------------------------------------------------ salary
# company words as in K.COMPANY_WORDS; a leading space there means 'starts a word', and 'INC' must be a whole word
# (' INC' used to hit 'INCORRECT ACCOUNT NUMBER')
COMPANY_RE = re.compile("|".join(r"\bINC\b" if w.strip() == "INC" else
                                 (r"\b" + re.escape(w.strip()) if w.startswith(" ") else re.escape(w))
                                 for w in K.COMPANY_WORDS))
TRUNC_SUFFIX_RE = re.compile(r"\s(?:LT|LTD|LIMI[A-Z]*|PVT|PRIVA[A-Z]*|PRIV)$")
LENDER_NAME_RE = re.compile(r"\b(?:FINANCE|FINANCIAL|FINSERVE?|FINSERVICES?|FINCORP|FINCAP|FINLEASE|FINVEST|LEASING|CAPITAL|INVESTMENTS?|CREDITS?|"
                            r"LENDING|LOANS?|NBFC|MICROFIN[A-Z]*)\b")
TRANSFER_RETURN_RE = re.compile(r"\b(?:NEFT|RTGS|IMPS|UPI)[\s\-/]*(?:RETURN|RTN)\b")

# salary keyword as banks print it: glued to a name / batch id / pay period ('AJAYSalary', 'BATCHID:0030SALARY',
# 'Salary_Feb25PAREKH') or cut at a field edge ('KV AUGUST SALAR'), plus the short forms of K.SALARY_RE
SALARY_KW_RE = re.compile(K.SALARY_RE + r"|SALARY|SALARIES|\bSALAR\b")
# payday-loan products named after salary - loan money, never pay: 'AMPIRE FINANCE .. A/C SALARY SET',
# 'ZED LEAFIN .. SALARY NOW', SalaryOnTime, EarlySalary, FlexSalary, SalaryBolt, Salarysetu, SALARYVIBE,
# 'ACH DR:SALARYTOPU..', PAISAONSALARY, 'SALARIED .. LOAN'
LOAN_PRODUCT_RE = re.compile(r"SALARY\s?(?:SET|NOW|ON\s?TIME|DOST|BOLT|VIBE|TOP\s?U|ADVANCE)|"
                             r"(?:EARLY|FLEX|PAISAON|ADVANCE)\s?SALAR|SALARIED\s.{0,30}LOAN")
# a credit that says it is a loan: 'LoanDisbursa', 'PL STF DISB', 'TRADOFINADISBURSA', 'Paymentdisbu'
LOAN_TEXT_RE = re.compile(r"LOAN|DISB")
# ICICI 'INF/<ref>/<remark>' = Internet funds transfer between the holder's own linked accounts (statement legend):
# 'NET BANKING INF/000179155321/Salary-<holder>' is the holder moving money with a self-typed remark
LINKED_INF_RE = re.compile(r"(?<![A-Z])INF/\d{6,}/")
OTHER_CHANNEL_RE = re.compile(r"NEFT|RTGS|IMPS|NACH|\bACH|\bCMS|INFT")
# payroll rails: a company paying pay without saying 'salary' does it by NEFT / RTGS / CMS / NACH credit
PAYROLL_CHANNEL_RE = re.compile(r"NEFT|RTGS|\bCMS|NACH[\s\-]*CR|\bACH[\s\-]*CR|\bINFT\b")
P2P_CHANNEL_RE = re.compile(r"IMPS|\bUPI\b|\bMMT\b|\bP2A\b")
# finance-company words in a payer name ('IKF HOME FINANCE', 'RICHMAN FINCAP', 'DAKSH LEFINS', 'U Y FIN',
# 'GOODSKILL SECURITIES', 'SAYYAM INVESTMENTS', 'RKBANSALFINANCE')
PAYER_LENDER_RE = re.compile(r"FINANCE|FINANCIAL|FINSERV|FINCORP|FINCAP|FINCON|FINLEAS|FINVEST|LEASING|LEFIN|CAPITAL|"
                             r"MICROFIN|NBFC|PORTFOLIO|\b(?:FIN|CREDITS?|INVESTMENTS?|SECURITIES|LENDING|LOANS?)\b")
# legal suffix cut short inside the narration ('TATA CONSUMER .. PRODUCTS LIMI') or at the end of the name
# ('SKYJUMPER SPORTS AND AMUSEMENTS P L', 'DELOITTE .. I P L')
BANK_NAME_RE = re.compile(r"\b[A-Z]+ SMALL FINANCE BANK\b|\bSMALL FIN(?:ANCE)? BANK\b|\bFINANCE BANK\b")
TRUNC_WORD_RE = re.compile(r"\b(?:LIMI|LIMIT|LIMITE|PRIVA|PRIVAT)\b")
P_L_SUFFIX_RE = re.compile(r"\sP\sL$")
_TIME_RE = re.compile(r"\b\d{1,2}:\d{2}(?::\d{2})?\b")  # SBM '23:05:42 Salary for April'
_GLUED_PERIOD_RE = re.compile(r"\b(SALARY|SAL)[\s_]*" + K._MONTHS_RE + r"'?\d{2,4}")
_PAGE_FOOTER_RE = re.compile(r"\s*HDFC BANK LIMITED$")
_MONTH_WORDS = {"JAN", "JANUARY", "FEB", "FEBRUARY", "MAR", "MARCH", "APR", "APRIL", "MAY", "JUN", "JUNE", "JUL",
                "JULY", "AUG", "AUGUST", "SEP", "SEPT", "SEPTEMBER", "OCT", "OCTOBER", "NOV", "NOVEMBER", "DEC",
                "DECEMBER"}
# posting labels that are not a payer: IDFC 'CMS_IFT', SCB 'BATCHID', Kotak 'NEFTINW', SBI 'BULK POSTING-BY SALARY',
# IDFC 'BLKIFT/ BLKPAY_07102025/FOR SALARY'
_NAME_NOISE = _REMARK_FILLER | _MONTH_WORDS | {"SALAR", "SALRY", "WAGES", "CMS", "IFT", "BATCHID", "NEFTINW", "BULK",
                                               "POSTING", "BLKIFT", "BLKPAY"}
_CMS_LABEL_RE = re.compile(r"\bCMS TRANSACTION\b")  # Yes / ICICI 'CMS TRANSACTION CMS/ SALARY/LARSEN AND TOUBRO..'
_REV_RE = re.compile(r"(?<![A-Z])REV(?![A-Z])")  # 'UPI-REV/MOB/../Rever sal Request' (a UPI reversal)
_GENERIC_PAYER = ({"THE", "MS", "M", "S", "P", "SHRI", "INDIA", "BANK", "LTD", "LIMITED", "PVT", "PRIVATE", "PRIV",
                   "PRI", "AND", "OF", "CO"} | set(K.IFSC_BANKS) | {c for c, _ in K.IFSC_BANKS.values()})


# IndusInd prints only the payroll remark: 'JAN 2026 NET PAY OF ECN <ref> NO.25192' - a pay period followed by
# 'NET PAY OF/FOR' at the start of the narration is a salary keyword; the rest (employee code, wrapped differently
# each month: 'ECN NO.' / 'EC NNO.') names no payer
NET_PAY_RE = re.compile(r"^\s*" + K._MONTHS_RE + r"\s?'?(?:20)?\d{2}\s+NET PAY OF(?![A-Z])")


def _has_salary_kw(u: str) -> bool:
    return bool(SALARY_KW_RE.search(LOAN_PRODUCT_RE.sub(" ", u)) or NET_PAY_RE.search(u))


def _salary_text(narr: str) -> str:
    """Narration with the noise that splits one employer into many names removed: page footer, time stamps,
    '_' joins and pay-period labels glued to the name ('Salary_March25PAREKH' -> 'SALARY PAREKH')."""
    u = _PAGE_FOOTER_RE.sub("", norm(narr))
    u = _CMS_LABEL_RE.sub(" ", _TIME_RE.sub(" ", u).replace("_", " "))
    u = re.sub(r"(?<=[A-Z0-9])(SALARY)", r" \1", u)
    u = re.sub(r"\bSALAR\b", "SALARY", u)  # remark cut at the field edge: 'KV AUGUST SALAR'
    return _GLUED_PERIOD_RE.sub(r" \1 ", u)


def _payer_name(name: str, u: str) -> str:
    ws = [w for w in name.split() if w not in _NAME_NOISE and not PERIOD_TOKEN_RE.match(w) and not w.isdigit()]
    # remitter-bank code left after the name: 'EAGLEVIEW SOL CHAS' next to 'CHASN52026073120022727' refs
    while len(ws) > 1 and len(ws[-1]) == 4 and ws[-1].isalpha() and (
            ws[-1] in K.IFSC_BANKS or re.search(r"\b" + ws[-1] + r"[A-Z]?\d{6,}", u)):
        ws.pop()
    return " ".join(ws)


def _payer_token(name: str) -> str:
    """First 10 letters of the payer from its first distinctive word: 'R K BANSAL FINANCE' -> 'RKBANSALFI',
    'FINCFRIENDS PRIVATE' -> 'FINCFRIEND'. Bank / legal words alone ('HDFC BANK') give no token."""
    ws = re.findall(r"[A-Z]+", name)
    while ws and ws[0] in _GENERIC_PAYER:
        ws.pop(0)
    tok = "".join(ws)[:10]
    return tok if len(tok) >= 8 else ""


def _first_word(name: str) -> str:
    """Distinctive first word (6+ letters) of a payer: 'PAWANSUT HOLDINGS LIMITED' / 'Pawansut MOBILE BANKING'."""
    ws = [w for w in re.findall(r"[A-Z]+", name) if w not in _GENERIC_PAYER]
    return ws[0] if ws and len(ws[0]) >= 6 else ""


def _pay_offset(d: date) -> int:
    """Day relative to month end: 31-Jul -> 0, 27-Jul -> -4, 3-Aug -> 3."""
    last = (date(d.year + d.month // 12, d.month % 12 + 1, 1) - timedelta(days=1)).day
    return d.day if d.day <= 15 else d.day - last


def _pay_period(d: date) -> str:
    """Pay month of a credit: pay that slips from the 30th to the 1st-10th of the next month ('01-Jul' after
    '30-May') still belongs to the month before."""
    return month_key(d - timedelta(days=10))


def _pay_cluster(ts: list[Txn]) -> tuple[float, dict, list[Txn]]:
    """An employer's regular pay among all its credits: the amount (+-25%) that carries the most money across
    pay months, and per pay month the credit nearest to it. Mid-month reimbursements / incentives, a part first
    month and bonus / arrears months fall outside the band ('steady')."""
    by_m: dict[str, list[Txn]] = defaultdict(list)
    for t in ts:
        by_m[_pay_period(t.date)].append(t)

    def hits(c: float) -> list[Txn]:
        near = (min(v, key=lambda x: abs(x.amount - c)) for v in by_m.values())
        return [t for t in near if abs(t.amount / c - 1) <= 0.25]
    scored = {c: hits(c) for c in {t.amount for t in ts}}
    # a band seen in at least half as many months as the most regular band; among those the most money
    # (pay 1.2L x 3 months beats a 15k reimbursement x 4; one 1.07L bonus month does not beat 20k x 3)
    need = (max(len(v) for v in scored.values()) + 1) // 2
    best = max((c for c in scored if len(scored[c]) >= need),
               key=lambda c: (sum(t.amount for t in scored[c]), len(scored[c]), c))
    center = statistics.median([t.amount for t in scored[best]])
    mains = {mk: min(v, key=lambda x: abs(x.amount - center)) for mk, v in by_m.items()}
    steady = sorted((t for t in mains.values() if abs(t.amount / center - 1) <= 0.25), key=lambda t: t.date)
    return center, mains, steady


# a remark that makes a fixed monthly company credit something other than pay ('RENT FEB 26', 'SWP', 'RETAINER',
# 'HOUSEHOLD', 'INV 221', 'COMMISSION', 'REIMB')
NON_PAY_REMARK_RE = re.compile(r"(?<![A-Z0-9])(?:RENT(?:AL)?|SWP|REDEMP[A-Z]*|MF|FEES?|RETAINER(?:SHIP)?|INV(?:OICE)?|"
                               r"BILLS?|HOUSEHOLD|COMMISSION|REIMB[A-Z]*)(?![A-Z])")


def _short_pay_ok(steady: list[Txn], narr: dict, txns: list[Txn], months_covered: float) -> bool:
    """Extra proof for the under-3-months company path, which rests on 2 credits and no salary keyword: 1.8+ months
    covered; an exact amount (pay is computed - HCL 66,384; rent, family support, retainers and SWPs are round: 18 of
    296 real salary credits are multiples of 500); no non-pay remark; and no lender / NBFC mandate taking a credit
    back within 35 days (a payday app's payout repaid to its NBFC partner: 'NEFT-ZIPCASH TECHNOLOGIES PVT LTD'
    20,000, then 'ACH D- RAZORPAYSOFTWAREPRIV-ZXFINVESTPD' 21,400)."""
    if months_covered < 1.8 or any(t.amount % 500 == 0 for t in steady):
        return False
    if any(NON_PAY_REMARK_RE.search(narr[id(t)]) for t in steady):
        return False
    for d in txns:
        u = f" {norm(d.narration)} "
        if d.direction != "DR" or not MANDATE_RE.search(u):
            continue
        if not (lender_name(u) or PAYER_LENDER_RE.search(BANK_NAME_RE.sub(" ", u))):
            continue
        if any(0 < (d.date - t.date).days <= 35 and 0.95 * t.amount <= d.amount <= 1.5 * t.amount for t in steady):
            return False
    return True


def detect_salary(txns: list[Txn], holder_tokens: set[str], months_covered: float, business: bool = False,
                  salary_account: bool = False) -> dict:
    # money the holder sends back to a payer = that payer is lending (Fincfriends, Sayyam, PSPR, Goodskill)
    # (transfers only: 'ATM CASH HINDU RAO HOSPITAL' is cash taken out at the employer's own premises)
    paid_to = [re.sub(r"[^A-Z]", "", norm(t.narration)) for t in txns if t.direction == "DR" and t.amount >= 1000
               and t.category not in ("Cash Withdrawal", "Card Spend", "Bank Charges")]
    # ... and a payer whose other credits say 'Loan' is a lender ('MMT/IMPS/../Loan/Pawansut ..')
    loan_cr = [re.sub(r"[^A-Z]", "", norm(t.narration)) for t in txns if t.direction == "CR"
               and (LOAN_TEXT_RE.search(norm(t.narration)) or LOAN_PRODUCT_RE.search(norm(t.narration)))]
    # the holder runs a payroll: salary transfers out to 3+ different people in one month ('NEFT/../Gitika Arora/
    # ../SALARY SEP 25' x 15). That is an employer's account - a 'FOR SALARY' credit into it is payroll funding
    # paid straight out to the staff, not the holder's income (same as a company-named holder)
    staff: dict[str, set[str]] = defaultdict(set)
    for t in txns:
        if t.direction == "DR" and t.amount >= 1000 and "self_transfer" not in t.tags and t.counterparty \
                and _has_salary_kw(norm(t.narration)) and not SALARY_KW_RE.search(t.counterparty):
            staff[month_key(t.date)].add(_ckey(t.counterparty))
    business = business or any(len(v) >= 3 for v in staff.values())
    groups: dict[str, list[Txn]] = defaultdict(list)
    pname: dict[int, str] = {}
    for t in txns:
        if t.direction != "CR" or t.amount < 3000 or "bounce" in t.tags:
            continue
        u = f" {norm(t.narration)} "
        if has_any(u, K.REVERSAL_WORDS) or has_any(u, K.INTEREST_WORDS) or "lender" in t.tags or _REV_RE.search(u):
            continue
        if TRANSFER_RETURN_RE.search(u):  # 'NEFT RETURN--KOUSHIK..': an outward transfer bouncing back
            continue
        if " REJECT" in u or "UNABLE TO PROCESS" in u:  # SCB 'IMPS REJECT|..|UNABLE TO PROCESS': own IMPS back
            continue
        if "PROVIDENT FUND" in u or "EPFO" in u or _is_self(t.counterparty, holder_tokens):
            continue
        if LOAN_PRODUCT_RE.search(u) or LOAN_TEXT_RE.search(u):
            continue
        if LINKED_INF_RE.search(u) and not OTHER_CHANNEL_RE.search(u):
            continue
        su = _salary_text(t.narration)
        name = "" if NET_PAY_RE.search(u) else _payer_name(counterparty(su), su)
        if _is_self(name, holder_tokens):
            continue
        key = "".join(w for w in re.findall(r"[A-Z]+", name) if w not in CHANNEL_TOKENS)[:14]
        key = key or ("SALARY" if _has_salary_kw(u) else "")
        if key:
            groups[key].append(t)
            pname[id(t)] = name if key != "SALARY" else ""
    # one employer printed with its name cut at different lengths ('PRAJVI VOY' / 'PRAJVI VOYAGES PRIVATE LIMITED')
    for a in sorted(groups, key=len):
        if len(a) < 8 or a not in groups:
            continue
        for b in [b for b in groups if b != a and b.startswith(a)]:
            groups[a].extend(groups.pop(b))
    # same employer, name cut at a different length because the pay-period label varies
    # ('SalaryJuly26 EAGLEVIEW SO' / 'SalaryAug26 EAGLEVIEW SOL'): merge salary-keyword groups sharing a 9-letter prefix
    kw_keys = sorted((k for k, ts in groups.items() if any(_has_salary_kw(norm(t.narration)) for t in ts)),
                     key=lambda k: -len(groups[k]))
    for i, a in enumerate(kw_keys):
        for b in kw_keys[i + 1:]:
            if a in groups and b in groups and len(a) >= 9 and len(b) >= 9 and a[:9] == b[:9]:
                groups[a].extend(groups.pop(b))
    cands = []
    for key, ts in groups.items():
        ts.sort(key=lambda t: t.date)
        center, mains, steady = _pay_cluster(ts)
        narr = {id(t): norm(t.narration) for t in ts}
        u_all = " ".join(narr.values())
        kw = _has_salary_kw(u_all)
        steady_ids = {id(t) for t in steady}
        # months shown: the regular pay; a bigger month (bonus / arrears) or one whose credit says salary too
        shown = sorted((t for t in mains.values() if id(t) in steady_ids or t.amount > center
                        or (kw and _has_salary_kw(narr[id(t)]))), key=lambda t: t.date)
        months = sorted({_pay_period(t.date) for t in shown})
        paid = [t for t in mains.values() if t.amount >= 0.75 * center]  # regular pay or more (bonus month)
        names = Counter(pname[id(t)] for t in steady if pname.get(id(t)))
        name = names.most_common(1)[0][0] if names else ""
        all_names = " ".join({pname.get(id(t), "") for t in ts})
        amts = [t.amount for t in steady]
        median = statistics.median(amts)
        cv = statistics.pstdev(amts) / median if len(amts) > 1 and median else 0.0
        offs = [_pay_offset(t.date) for t in steady]
        spread = max(offs) - min(offs)
        days = [t.date.day for t in steady]
        day_sd = statistics.pstdev(days) if len(days) > 1 else 0
        # the remitting bank's own legal name ('HDFC BANK LTD', 'AU SMALL FINANCE BANK LIMITED') says nothing
        # about the payer; a payer name cut inside its legal suffix ('BRISTLECONE INDIA LT') still does
        company = bool(COMPANY_RE.search(re.sub(r"\bBANK\s+(?:LTD|LIMITED)\b", "BANK", u_all))
                       or TRUNC_WORD_RE.search(u_all)
                       or any(TRUNC_SUFFIX_RE.search(pname.get(id(t)) or "") or P_L_SUFFIX_RE.search(pname.get(id(t)) or "")
                              for t in ts))
        payroll_rail = all(PAYROLL_CHANNEL_RE.search(narr[id(t)]) and not P2P_CHANNEL_RE.search(narr[id(t)])
                           for t in steady)
        # lender evidence (a credit that says 'salary' is judged on its payer's name only): the holder repays this
        # payer, its other credits say 'Loan', or a finance-company name - which an employer that is a finance
        # company (IKF HOME FINANCE paying 98,792 every month-end by NEFT) clears only with a fixed payroll:
        # 3+ months, every credit within 3% and on the same pay day (+-3), by NEFT/RTGS/CMS
        tok, fw = _payer_token(name or all_names), _first_word(name or all_names)
        repaid = not kw and bool(tok) and any(tok in d for d in paid_to)
        loan_payer = not kw and bool(fw) and any(fw in d for d in loan_cr)
        fixed_payroll = (payroll_rail and len(steady) >= 3 and spread <= 3
                         and all(abs(a / median - 1) <= 0.03 for a in amts))
        # with no salary keyword the payer's whole narration counts ('NEFT/TRISTAR FINCON CREDIT PVT ..' is cut to
        # 'TRISTAR FINCON PVT'); the remitting bank's name ('AU SMALL FINANCE BANK') is not the payer
        lend_txt = all_names if kw else all_names + " " + BANK_NAME_RE.sub(" ", u_all)
        lender = repaid or loan_payer or (bool(PAYER_LENDER_RE.search(lend_txt)) and not fixed_payroll)
        neft = any(("NEFT" in n or "RTGS" in n or "IMPS" in n) for n in narr.values())
        upi_only = all("UPI" in n for n in narr.values())
        per_month = Counter(month_key(t.date) for t in ts)
        score = 0
        score += 40 if kw else 0
        score += 15 if company else 0
        score += 10 if neft else 0
        score -= 15 if (upi_only and not kw) else 0
        score += 20 if len(months) >= 2 else 0
        score += 10 if len(months) >= 3 else 0
        score += 10 if cv < 0.25 else (-10 if cv > 0.6 else 0)
        score += 5 if day_sd <= 5 else 0
        score -= 10 if max(per_month.values()) > 2 else 0
        score += 5 if median >= 10000 else 0
        score -= 40 if lender else 0
        # income drives the loan amount. A credit is pay when the narration says so (not a loan product, not a
        # lender), or - with no keyword - when a company pays by NEFT/RTGS/CMS around month end (-8..+10 days,
        # pay day within 7) the regular amount (+-25%) or more in 3+ pay months, 2+ of them at the regular level;
        # a payer with no company word ('Adecco NEFT-') needs 4+ regular months on a fixed pay day (+-3). A
        # person's monthly IMPS/UPI ('SOLOMON C', 'ROHITLOURE') may be family, rent or a private loan; a monthly
        # IMPS from a company is how payday lenders roll loans; 2 months of NEFT is not proof.
        joined, first, acct_pay, short = False, None, False, False
        if kw:
            # postings that say salary but name no payer and never repeat an amount across their pay months
            # (SBI 'BULK POSTING-BY SALARY' 68,088 one month, 9,600 + 11,776 + 8,832 the next) are arrears /
            # allowance batches: no regular monthly pay is proven, so they are not the salary
            ok = not lender and not (key == "SALARY" and len(months) >= 2 and len(steady) == 1)
        else:
            person = bool(re.match(r"(?:MR|MRS|MS|SHRI|SMT|DR)\b", name))  # 'MR ARUNA KUMAR ROUTRAY'
            # joining month: the first pay month is a part-month credit (smaller) from the same company on the same
            # rail and pay day, followed by full months in a row ('INFOCAREER' 13,430 then 58,587 / 58,691)
            pm = sorted(mains)
            first = mains[pm[0]] if pm else None
            joined = (company and len(pm) >= 3 and len(paid) == len(pm) - 1 and first is not None
                      and first.amount < 0.75 * center and id(first) not in steady_ids
                      and bool(PAYROLL_CHANNEL_RE.search(narr[id(first)]))
                      and not P2P_CHANNEL_RE.search(narr[id(first)])
                      and abs(_pay_offset(first.date) - statistics.median(offs)) <= 7
                      and all(_next_month(a) == b for a, b in zip(pm, pm[1:])))
            regular = ((len(paid) >= 3 and len(steady) >= 2) or (joined and len(steady) >= 2) if company
                       else (len(steady) >= 4 and spread <= 3 and not person))
            if (company and not regular and months_covered < 3 and len(steady) >= 2 and len(steady) == len(mains)
                    and spread <= 3 and all(abs(a / median - 1) <= 0.03 for a in amts)):
                # a statement under 3 months cannot show 3 pay months: a company paying the same amount (+-3%) on
                # the same pay day (+-3) in EVERY pay month whose pay day the statement covers (IDBI 1-Aug..19-Oct:
                # 'NEFT-..-HCL TECHNOLOGIES LIMITED' 66,384 on 31-Aug and 30-Sep) is regular. Rail, offsets and
                # lender checks below still apply
                lo, hi = min(t.date for t in txns), max(t.date for t in txns)
                o = round(statistics.median(offs))
                due, y, mth = set(), (lo.year if lo.month > 1 else lo.year - 1), (lo.month - 1 or 12)
                while (y, mth) <= (hi.year, hi.month):
                    month_end = date(y + mth // 12, mth % 12 + 1, 1) - timedelta(days=1)
                    if lo <= month_end + timedelta(days=o) <= hi:  # this month's pay day lies inside the statement
                        due.add(month_key(month_end))
                    y, mth = (y, mth + 1) if mth < 12 else (y + 1, 1)
                # 2 credits alone also fit rent, family support, retainers, SWPs and payday payouts (see
                # _short_pay_ok); a salary resting on this path is flagged SALARY_SHORT_HISTORY
                regular = short = (due == {_pay_period(t.date) for t in steady}
                                   and _short_pay_ok(steady, narr, txns, months_covered))
            ok = (not lender and regular and payroll_rail and all(-8 <= o <= 10 for o in offs) and spread <= 7)
            # the bank's own header names the account a salary account ('Account Variant/ Description: SA - SMART
            # SALARY EXCLUSIVE'): the employer's payroll is what this account is for, so the label stands in for
            # the salary keyword - for a payer that pays the regular amount in EVERY pay month (2+), by
            # NEFT/RTGS/CMS, around month end (-8..+10) on a fixed pay day (+-3). Used only when nothing else
            # qualifies (Yes 'CMS-TPT-.. AEGIS CUSTOMER SUP' 57,175 on 30-Dec, 57,200 on 31-Jan)
            acct_pay = (salary_account and not ok and not lender and not person and payroll_rail
                        and len(months) >= 2 and len(steady) == len(mains)
                        and all(-8 <= o <= 10 for o in offs) and spread <= 3)
            score += 40 if acct_pay else 0
        # every pay month the employer paid, not only the months inside the +-25% band: a pay month's credit on
        # the usual pay day (+-3 days of the steady credits), on the same rail, of at least half the regular
        # amount is that month's pay too (loss-of-pay month, lower variable pay, pay before a raise). The monthly
        # figure is the median of the steady AND these months - leaving the low months out overstates income
        # (TBSB 35-40k for 7 months, 52-57k for 5: the band alone said 51,894). A bonus month above the band, a
        # joining month's part pay and mid-month extras stay out of the median. Scoring above is unchanged.
        lo_o, hi_o = min(offs) - 3, max(offs) + 3

        def low_pay(t: Txn) -> bool:
            return (id(t) not in steady_ids and 0.5 * center <= t.amount < center
                    and lo_o <= _pay_offset(t.date) <= hi_o and not (joined and t is first)
                    and (not payroll_rail or (bool(PAYROLL_CHANNEL_RE.search(narr[id(t)]))
                                              and not P2P_CHANNEL_RE.search(narr[id(t)]))))
        shown_ids = {id(t) for t in shown}
        shown = sorted(shown + [t for t in mains.values() if id(t) not in shown_ids and low_pay(t)],
                       key=lambda t: t.date)
        lows = [t.amount for t in shown if low_pay(t)]
        if lows:
            median = statistics.median(amts + lows)
        cands.append({"key": key, "name": name or "Not named in narration", "score": score, "months": months,
                      "avg_amount": round(median, 2), "shown": shown, "steady": steady, "kw": kw, "ok": ok,
                      "rail": payroll_rail, "lender": lender, "acct_pay": acct_pay, "short": short and ok})
    cands.sort(key=lambda c: (-c["score"], -c["avg_amount"]))
    # a company's own account (holder 'X PVT LTD') has no payroll income: its regular credits are trade receipts
    winners = [c for c in cands if not business and c["score"] >= 55 and c["ok"]]
    if not winners and not business:  # salary account: the one regular month-end payroll credit (see acct_pay)
        winners = [c for c in cands if c["acct_pay"]][:1]
    out = {"detected": False, "confidence": 0, "employer": None, "avg_amount": 0.0, "usual_day": None, "months": [],
           "short_history": False, "candidates": [{"name": c["name"], "score": c["score"], "months": len(c["months"]),
                           "avg_amount": c["avg_amount"]} for c in cands[:5]]}
    if winners:
        best = winners[0]
        # job change: another salary-keyword employer paying wholly before or after the best one, by a payroll
        # rail or for 2+ months (a one-off UPI 'sal' remark is not an employer)
        chosen = [best] + [c for c in winners[1:] if c["kw"] and (c["rail"] or len(c["months"]) >= 2)
                           and (c["months"][-1] < best["months"][0] or c["months"][0] > best["months"][-1])][:2]
        cur = max(chosen, key=lambda c: c["months"][-1])  # the employer paying now
        ts = sorted((t for c in chosen for t in c["shown"]), key=lambda t: t.date)
        for t in ts:
            t.tags.append("salary")
            t.category = "Salary"
        out.update({
            "detected": True, "confidence": int(max(0, min(100, cur["score"]))), "employer": cur["name"],
            # regular monthly pay = median of the steady credits; bonus / arrears / part months stay out
            "avg_amount": cur["avg_amount"],
            "usual_day": int(statistics.median([t.date.day for t in cur["steady"]])),
            "months": [{"month": month_key(t.date), "amount": round(t.amount, 2), "date": t.date.isoformat(),
                        "narration": t.narration[:100]} for t in ts],
            "short_history": any(c["short"] for c in chosen),  # rests on the under-3-months company path
        })
    # 'Salary' category only on credits accepted as pay (not 'SALARY SET' loans, linked-account 'Salary-' moves)
    for t in txns:
        if t.direction == "CR" and t.category == "Salary" and "salary" not in t.tags:
            t.category = categorize(t, salary_ok=False)[0]
    return out



# ------------------------------------------------------------------ loans
# Lender identification beyond the brand dictionary (K.LENDERS). Only names that the narration itself proves:
#  - a payer/payee name ending in a finance-company word ('AMPIRE FINANCE', 'NAMAN FINLEASE', 'SOLOMON CAPITAL'),
#    spaced, glued ('RKBANSALFINANCE', 'DIGNERFINLEAS', VPA 'solomoncapital.payu@'), or cut at the field end
#    ('NAMAN FIN/'); CREDIT / LOANS / LENDING / INVESTMENTS only with a company suffix ('SAYYAM INVESTMENTS PVT')
#  - a credit that says it is a loan payout ('LOANDISBURSAL', 'PAYMENTDISBURSED', 'Amount Disbursed')
#  - a counterparty already proven a lender elsewhere in the same statement (same name, other leg)
# Bank names ('AU SMALL FINANCE BANK') are never lenders by this rule.
# 'LOANS' (plural, a brand: 'Naukri Loans', 'Shatabdi Loans') yes; singular 'LOAN' is a remark ('LOAN REPAYMENT')
_LSTEM = (r"FINANCE|FINANCIAL|FINSERVE?|FINSERVICES?|FINCORP|FINCAP|FINLEASE|FIN-LEASE|FIN LEASE|FINVEST|LEASING|"
          r"LEAFIN|MICROFIN[A-Z]*|PORTFOLIO|CAPITALS?|LOANS")
SPACED_LENDER_RE = re.compile(r"((?:[A-Z][A-Z&.']*\s){1,3})(?:" + _LSTEM + r")(?![A-Z])")
CUT_LENDER_RE = re.compile(r"((?:[A-Z][A-Z&.']*\s){1,3})FIN(?:A|AN|ANC)?(?=\s?(?:[/\-:|]|$))")
SUFFIXED_LENDER_RE = re.compile(r"((?:[A-Z][A-Z&.']*\s){1,3})(?:CREDITS?|LOANS?|LENDING|INVESTMENTS?)\s"
                                r"(?:PVT|PRIVATE|PRIVA[A-Z]*|LTD|LIMITED|LIMI[A-Z]*|P)(?![A-Z])")
GLUED_LENDER_RE = re.compile(r"(?<![A-Z])([A-Z]{3,}?)(?:FINANCE|FINANC|FINAN(?![A-Z])|FINLEASE?|FINLEAS|FINCAP|FINSERVE?|FINCORP|"
                             r"FINVEST|LEASING|LEAFIN|CAPITALS?|CAPIT|LOANS?)[A-Z]*(?![A-Z])")
DISB_WORD_RE = re.compile(K.DISB_RE)
LOAN_APP_RE = re.compile("|".join(re.escape(w) for w in sorted(K.LOAN_APPS, key=len, reverse=True)))
_NAME_JUNK = CHANNEL_TOKENS | {"LOAN", "LOANS", "REPAYMENT", "REPAY", "EMI", "FOR", "OF", "TOWARDS", "AC", "ACH", "NACH",
                               "TP", "LTD", "LIMITED", "PVT", "PRIVATE", "MOBILE", "BANKING", "NET", "AND", "THE",
                               "FUND", "AMOUNT", "DISBURSED", "DISBURSEMENT", "API", "PL", "Q", "AT", "MR", "MRS", "MS"}
_GLUE_GENERIC = ("PERSONAL", "HOME", "CAR", "AUTO", "GOLD", "BIKE", "EDU", "EDUCATION", "BUSINESS", "FOR", "REPAY",
                 "PAYMENT", "PAY", "MY", "THE", "TERM", "SALARY", "OVERDUE", "SMALL", "TOWARDS", "CASH", "INSTANT",
                 "EMERGENCY", "SHORT", "HAND", "OF", "TO", "THIS", "YOUR", "VENTURE", "WORKING", "MARKET")
_NOT_LENDER_WORDS = {"BANK", "SMALL", "INSURANCE", "MUTUAL", "BROKING", "STOCK", "SHARE", "SHARES", "ASSET", "AMC",
                     "VENTURE", "VENTURES", "MARKETS", "MARKET", "SECURITIES", "EQUITY", "WEALTH", "GEOJIT"}
_KEY_TAIL = {"PVT", "PRIVATE", "PRIVA", "PRIV", "PRI", "PR", "P", "LTD", "LIMITED", "LIMITE", "LIMIT", "LIMI", "LIM",
             "LI", "L", "LT", "INDIA", "I", "CO", "COMPANY", "AND", "OF", "THE", "SERVICES", "SERVICE", "SERVIC", "SERV",
             "SER", "SE", "S", "AC", "IN", "FU", "F", "FI", "C", "CA", "CAP", "HOUSING"}
_KEY_STEM_RE = re.compile(r"(?:FINANCE|FINANCIAL|FINANCI|FINANC|FINAN|FINA|FINLEASE?|FINLEAS|FINCAP|FINSERVE?|"
                          r"FINSERVICES?|FINCORP|FINVEST|LEASING|LEAFIN|MICROFIN[A-Z]*|PORTFOLIO|CAPITALS?|CAPIT|"
                          r"LOANS?|CREDITS?|LENDING|INVESTMENTS?|FIN)$")
_KEY_CUT_RE = re.compile(r"FINANC|FINLEAS|FINCAP|FINSERV|FINCORP|FINVEST|LEASING|LEAS|CAPITAL|CREDIT|PORTFOLIO|"
                         r"MICROFIN|LOANS")
_AGG_RE = re.compile("|".join(re.escape(w) for w in sorted(K.AGGREGATOR_WORDS, key=len, reverse=True)))
# the bank's own loan account debited: HDFC 'EMI 145891647 CHQ ..', ICICI 'Personal Loan XX55021 EMI' / 'SMP/LPBNGXX55021'
OWN_LOAN_RE = re.compile(r"^\s*EMI (\d{6,})\b|\b(?:PERSONAL|AUTO|HOME|CAR|GOLD|BUSINESS|TWO WHEELER) LOAN "
                         r"(?:A/C )?XX(\d{4,})|(?<![A-Z])L[A-Z]{1,4}XX(\d{4,})")
UPI_MANDATE_RE = re.compile(r"MANDATE|AUTOPAY|AUTO PAY|RECURRING")
# mandate words as whole tokens ('NACH' must not hit 'NACHIKET', 'SI-' not 'DEWASI-' / 'HOUSI-')
MANDATE_RE = re.compile(r"(?<![A-Z])(?:E-?NACH|NACH|ACH|ECS)(?![A-Z])|(?<![A-Z])ACH(?:_DR|D-|INWDR)|ACHINWDR|"
                        r"MANDATE|AUTOPAY|AUTO PAY|\bSI DEBIT\b|STANDING INSTRUCTION|(?<![A-Z])SI-")
EMI_WORD_RE = re.compile(r"(?<![A-Z])EMI(?![A-Z])|EMI ?PAYMENT|EMIDUES|INSTAL+MENT")
_TIDY_DROP = {"ACCOUNT", "ESCROW", "AC", "A/C", "AMOUNT", "LOAN", "PAYMENT", "API", "TXN", "FUND", "TRANSFER"}
_TIDY_CUT = {"RSEMENT", "URSEMENT", "SEMENT", "EMENT", "NT", "AL", "SAL"}  # tails of a split 'DISBU RSEMENT'
_BANK_LEAD = {"IDFC", "HDFC", "ICICI", "SBI", "AXIS", "KOTAK", "YES", "RBL", "IDBI", "INDUSIND", "BANK"}
OVERDUE_RE = re.compile(r"\bOVERDUE\b|\bPENAL\b|\bLPP\b|LATE PAYMENT")
REV_CREDIT_RE = re.compile(r"\b(?:ACH ?D-|ACH ?DR\b|ACH_DR|NACH ?DR\b|NACH_DR|ECS ?DR\b|ACHINWDR|ACH ?DEBIT)")
BIG_REF_RE = re.compile(r"\d{8,}")
EMBED_DATE_RE = re.compile(r"(?<!\d)(?:(20\d{2})(\d{2})(\d{2})|(\d{2})(\d{2})(20\d{2}))(?!\d)")


_GENERIC_NAME = {"HOME", "HOUSING", "GOLD", "PERSONAL", "VEHICLE", "AUTO", "CONSUMER", "MICRO", "RURAL", "INDIA",
                 "INDIAN", "CAR", "BIKE", "BUSINESS", "CASH", "INSTANT", "EASY", "QUICK", "DIGITAL", "SMART"}


def _clean_name(words: list[str]) -> str | None:
    ws = [w for w in words if not re.search(r"\d", w) and w not in ("NET", "BANKING", "MOBILE")]
    # misaligned rows glue the remitting bank to the name: 'IDFC BANKNAMAN FINLEASE' -> 'NAMAN'
    ws = [w[4:] if w.startswith("BANK") and len(w) > 6 else w for w in ws]
    ws = [re.sub(r"^[A-Z]\.(?=[A-Z]{3})", "", w) for w in ws]  # 'S.AMAN' (M/S. prefix glued) -> 'AMAN'
    if ws[:2] == ["M", "S"]:
        ws = ws[2:]
    while ws and (ws[0].strip(".&'") in _NAME_JUNK or ws[0] in _BANK_LEAD or len(ws[0].strip(".&'")) == 0):
        ws.pop(0)
    if not ws or any(w.strip(".&'") in _NOT_LENDER_WORDS for w in ws) or all(w in _GENERIC_NAME for w in ws):
        return None
    return " ".join(ws)


def _lender_name_in(u: str) -> str | None:
    for rx in (SPACED_LENDER_RE, SUFFIXED_LENDER_RE, CUT_LENDER_RE):
        for m in rx.finditer(u):
            name = _clean_name(m.group(1).split())
            if name and sum(ch.isalpha() for ch in name) >= 3:
                stem = m.group(0)[len(m.group(1)):].split()[0]
                return f"{name} {stem}"[:40]
    for m in GLUED_LENDER_RE.finditer(u):
        tok = m.group(0)
        tok = _AGG_RE.sub("", tok) or tok
        tok = re.sub(r"^(?:IMPS|NEFT|RTGS|UPI|ICICI|HDFC|IDFC|AXIS|SBI|YESB|RUPAYECOM)+", "", tok) or tok
        g = GLUED_LENDER_RE.fullmatch(tok)
        pre = g.group(1) if g else ""
        if len(pre) < 3 or pre.startswith(_GLUE_GENERIC) or pre.endswith(_GLUE_GENERIC) or "BANK" in tok \
                or "SMALL" in pre or pre in _NOT_LENDER_WORDS or pre in CHANNEL_TOKENS:
            continue
        # '<brand>LOAN(S)' is a loan app only as a UPI payee ('BIGROCKLOAN', 'SQUIDLOANS'); in an IMPS remark it is
        # a person's note ('DINESHKUMARLOAN/Mr Dinesh')
        if re.fullmatch(r"[A-Z]+LOANS?[A-Z]*", tok) and not re.search(r"FIN|CAPIT|LEAS", tok) \
                and "UPI" not in u and "@" not in u:
            continue
        return tok[:40]
    return None


# UPI payee field cut at a fixed width by the bank (Union Bank 'UPIAR/../DR/BAJAJ FI/HDFC/bflautopay@hdf' = 8 chars)
_UPI_PAYEE_RE = re.compile(r"/(?:DR|CR)/\s*([A-Z][A-Z0-9 &.]{5,9}?)\s*/[A-Z]{4}/")
_BRANDS = sorted({w.strip() for w in K.LENDERS if len(w.strip()) >= 8}, key=len)


def _cut_brand(u: str) -> str | None:
    """A dictionary lender whose name the bank cut short in the UPI payee field: the field (7+ letters, at
    least one word) is the start of exactly one-brand's name ('BAJAJ FI' -> 'BAJAJ FIN')."""
    m = _UPI_PAYEE_RE.search(u)
    if not m:
        return None
    f = re.sub(r"\s+", " ", m.group(1)).strip()
    if len(f.replace(" ", "")) < 7:
        return None
    hits = {b for b in _BRANDS if b.startswith(f) and b != f}
    return min(hits, key=len) if hits else None


_LENDER_ALIAS = {"BFLAUTOPAY": "BAJAJ FIN"}  # a lender's payment handle -> its name


def lender_name(u: str) -> str | None:
    """Lender named in a normalised narration (' ... ' padded), or None. Dictionary brands first."""
    hit = find_lender(u)
    if hit:
        return _LENDER_ALIAS.get(hit.strip(), hit.strip())
    hit = LOAN_APP_RE.search(u)
    if hit:
        return hit.group(0)
    hit = _cut_brand(u)
    if hit:
        return hit
    # second try without reference numbers spliced into the name ('Ampire IMPS-621716271071 Fin/KKBK')
    return _lender_name_in(u) or _lender_name_in(re.sub(r"\s+", " ", re.sub(r"[A-Z]*-?\d{6,}", " ", u)))


def _tidy(name: str) -> str:
    """Display name: 'FOCUS INDU LOANDISBURSAL' -> 'FOCUS INDU', 'SHATABDI LEAPROFIN PRIVATE LIMITED' -> 'SHATABDI LEAPROFIN'."""
    s = re.sub(r"\s*HDFC BANK(?: LIMITED| LTD)?\s*$", "", norm(name))  # HDFC page footer leaking into the field
    s = re.sub(r"(?<=[A-Z])-[A-Z]\b", "", s)  # IMPS bank-code tail 'HOLDINGS LI-I'
    s = re.sub(r"\s+TO\s+.*$", "", s)  # 'Gaurav Rak to AMIT KE' (payer to payee)
    s = re.sub(r"^M S\s+(?=\S{3})", "", s)  # 'M/S' split by the field slash: 'M S LENDEN' -> 'LENDEN'
    ws = [w for w in s.split() if not DISB_WORD_RE.search(w) and w not in _TIDY_DROP and w not in _TIDY_CUT
          and not re.search(r"\d|XXX", w)]
    while len(ws) > 1 and (ws[0] in CHANNEL_TOKENS or ws[0] in ("AND", "OF", "THE", "MAS", "M/S")):
        ws.pop(0)
    while len(ws) > 1 and ws[-1] in _KEY_TAIL:
        ws.pop()
    out = " ".join(ws).strip(" -/")
    return re.sub(r"DISBURS[A-Z]*$|DISBU$", "", out).strip() or name.strip()


def _payer_ok(c: str) -> bool:
    if not c or re.search(r"\d|XXX|OFFICE|BRANCH|BANK", c):
        return False
    return any(len(w) >= 4 and w not in CHANNEL_TOKENS and w not in _BANK_LEAD and w not in _TIDY_DROP
               and w[:4] not in K.IFSC_BANKS for w in c.split())


def _disb_payer(t: Txn) -> str | None:
    """Payer of a credit that says 'loan disbursed': the name in the disbursal field itself
    ('HARSH TRACOM PRIVATE LIMITED DISBURSEMENT ACCOUNT') or else the counterparty of the other fields
    ('GAGANDEEP SERVICES P - LOANDISBURSAL'). Never the holder ('PL STF DISB-<holder>')."""
    narr = norm(t.narration)
    fields = [f.strip() for f in re.split(r"[/|:]+|-", narr) if f.strip()]
    a = ""
    rest = []
    for f in fields:
        if DISB_WORD_RE.search(f) and not a:
            ws = [w for w in f.split() if not DISB_WORD_RE.search(w) and w not in _TIDY_DROP and w not in ("PL",)
                  and not re.search(r"\d", w)]
            a = " ".join(ws)
        else:
            rest.append(f)
    a_company = bool(a) and bool(COMPANY_RE.search(a) or TRUNC_SUFFIX_RE.search(a))
    a = _tidy(a) if a else ""
    b = counterparty(" / ".join(rest)) if rest else ""
    b = _tidy(b) if b else ""
    if "self_transfer" in t.tags:
        b = ""  # the other fields name the holder
    if _payer_ok(a) and (a_company or len([w for w in a.split() if len(w) >= 3]) >= 2):
        return a
    if _payer_ok(b):
        return b
    return a if _payer_ok(a) else None


def lender_key(name: str) -> str:
    """'AMPIRE FINANCE PVT' / 'Ampire Finan' / 'AMPIREFINANCE' -> 'AMPIRE'; 'BAJAJ FIN' / 'BAJAJFIN' -> 'BAJAJ'."""
    ws = re.findall(r"[A-Z]+", norm(name))
    while len(ws) > 1 and (ws[-1] in _KEY_TAIL or _KEY_STEM_RE.fullmatch(ws[-1])):
        ws.pop()
    k = "".join(ws)
    k = _AGG_RE.sub("", k) or k
    # glued sub-merchant with a varying tail: 'DMIFINANCESX' / 'DMIFINANCET9' -> 'DMI', 'KASARCREDITANDCA' -> 'KASAR'
    m = _KEY_CUT_RE.search(k, 3)
    if m:
        k = k[:m.start()]
    for _ in range(3):
        k2 = _KEY_STEM_RE.sub("", k)
        if len(k2) < 3:
            break
        k = k2
    return k[:14]


def _keys_same(a: str, b: str) -> bool:
    if a == b:
        return True
    s, l = sorted((a, b), key=len)
    # a cut name ('SOLOMONCAPI') or a prefixed one ('CSHFREAVINASH' / 'AVINASH'); 'BRANCH' (Indian Bank's
    # '/BRANCH : ..' suffix) must not join 'BRANCHINTER'
    if len(s) < 6:
        return False
    return (l.startswith(s) and len(s) >= 0.6 * len(l)) or l.endswith(s)


def _mandate_party(u: str) -> str | None:
    """Sub-merchant of an aggregator-collected mandate: 'RAZORPAYSOFTWAREPRIV-DMIFINANCESX' -> 'DMIFINANCE',
    'CTRAZORPAY-PIRAMALCAPMWU9..' -> 'PIRAMALCAP', 'ACH DR:MONEYMINTRTL1EYE' -> 'MONEYMINTR'."""
    m = _AGG_RE.search(u)
    if m:
        rest = u[m.end():]
        for tok in re.findall(r"[A-Z][A-Z0-9]{3,}", rest):
            lead = re.match(r"[A-Z]+", tok).group(0)
            if len(lead) >= 4 and lead not in CHANNEL_TOKENS and not _AGG_RE.fullmatch(lead) and lead not in (
                    "PRIV", "PRIVATE", "SOFTWARE", "PAYMENTS", "TPZ", "CLG", "HDFC", "LIMITED") \
                    and not lead.startswith("PAYMEN"):
                return lead[:10]
        return re.sub(r"[^A-Z]", "", m.group(0))[:20]  # 'ACHInwDr-CTRAZORPAY/28-02-2026': only the aggregator
    m = re.search(r"\bACH ?DR:([A-Z]{4,})", u)
    return m.group(1)[:10] if m else None


def _own_loan(u: str, charge_ok: bool = False) -> str | None:
    """Loan account number in the narration. A charge row ('LABNGXX49218-BOUNCECHGSJUN25') names the loan too,
    but is only used with charge_ok (a bounce on that loan, not an instalment)."""
    m = OWN_LOAN_RE.search(u)
    if not m or (not charge_ok and has_any(u, K.CHARGE_WORDS)):
        return None
    return next(g for g in m.groups() if g)


# mandate reference (UMRN, 16 digits) after the sponsor-bank code: 'ACH/IDFC FIRST BANK/ICIC7020403252010967/..',
# 'SBIN7010705252069669', 'ICIC0000000013530290'; in an ICICI return code it ends the digit run after the return
# no. and date: 'ECSRTN1_0305257020403252010967GST', 'ECSRTN4_1002250000000013530290'
UMRN_RE = re.compile(r"(?<![A-Z])[A-Z]{4}(\d{16})(?!\d)")
ECS_UMRN_RE = re.compile(r"ECSRTN\d?_?[\d ]*?(\d{16})(?!\d)")
# PSB prints a 'S<7-9 digits>' token inside the payee field, splitting a word: 'UPI/DR/1099../DM S47018611 I Finance
# Pvt ltd' and 'D S89546673 MI Finance' are both 'DMI Finance'
INB_EMANDATE_RE = re.compile(r"(?<![A-Z])INB E[\s-]?MANDATE(?![A-Z])")
PSB_SPLICE_RE = re.compile(r"(?<=/)([A-Z]{1,4}) S\d{6,9} (?=[A-Z])")
# credit-card issuers in the lender list: their bill payments are lender debits only when the narration says loan / EMI
CARD_ISSUERS = ("SLICE",)
# Axis recovers its own loan from the account in parts whenever money lands: 'UCR086110660118_EMI_10-02-2025' 45,128
# + '.._EMI_12-02-2025' 604. The loan a/c number + EMI + recovery date; the instalment is the month's total
PART_RECOVERY_RE = re.compile(r"^\s*([A-Z]{2,4}\d{9,})_EMI_\d{2}-\d{2}-\d{4}(?!\d)")
# a bank collecting its loan by NACH from this account, the loan number printed after its name (Axis
# 'ACH-DR-HDFC BANK LTD-414097540- UTIB7020..'): that loan account's instalment
BANK_LOAN_ACH_RE = re.compile(r"^\s*ACH-DR-([A-Z][A-Z ]{1,20}? BANK(?: LTD| LIMITED)?)-\s?(\d{8,12})-")
# ICICI corporate-collection debit on a loan number ('CMS/001351057234/SMSOTP__33091 4961753') and the NACH return
# charge that quotes the same loan number ('NACH_AD_RTN_CHRG~Of Bnk BFLo~02APR24~8523c9d~33091') or the lender's
# name cut short ('NACH_AD_RTN_CHRG~INGENOT~1 0MAY24~6f078aa~ADITYA BI')
CMS_LOAN_RE = re.compile(r"(?<![A-Z])CMS/\d{6,}/([A-Z]{3,})__(\d{5,})(?: (\d{4,}))?\s*$")
NACH_RTN_ID_RE = re.compile(r"(?<![A-Z])NACH_AD_RTN_CHRG~[^~]*~[^~]*~[^~]*~(\d{5,})\s*$")
NACH_RTN_NAME_RE = re.compile(r"(?<![A-Z])NACH_AD_RTN_CHRG~[^~]*~[^~]*~[^~]*~([A-Z][A-Z ]{5,24}?)\s*$")
# IOB ECS debit: 'BDECS-CTRAZORPAY- TRF IOBA7010509252000762' (party, then the mandate's UMRN)
BDECS_RE = re.compile(r"^\s*BDECS-([A-Z][A-Z ]{2,30}?)-\s*TRF\s+([A-Z]{4}\d{16})(?!\d)")


def _mandate_id(u: str) -> str | None:
    m = ECS_UMRN_RE.search(u) or UMRN_RE.search(u)
    return m[1] if m and m[1].strip("0") else None


def _reversal_pairs(txns: list[Txn], U: dict) -> tuple[set, set]:
    """A returned debit and its reversal: same amount, credit 0-5 days after, and the credit either repeats the
    debit's mandate narration ('ACH D- BAJAJ FINANCE LTD-P400..' as a CREDIT) or shares an 8+ digit reference /
    the whole narration. Neither leg is money really lent or repaid."""
    by_amt: dict[float, list[Txn]] = defaultdict(list)
    for t in txns:
        if t.direction == "DR":
            by_amt[round(t.amount, 2)].append(t)
    rev_cr, rev_dr = set(), set()
    for c in txns:
        if c.direction != "CR":
            continue
        uc = U[id(c)]
        refs_c = set(BIG_REF_RE.findall(uc))
        core_c = re.sub(r"[^A-Z]", "", uc)
        mandate_like = bool(REV_CREDIT_RE.search(uc))
        for d in by_amt.get(round(c.amount, 2), ()):
            if id(d) in rev_dr or not (0 <= (c.date - d.date).days <= 5):
                continue
            ud = U[id(d)]
            same = (refs_c & set(BIG_REF_RE.findall(ud))) or (core_c and core_c == re.sub(r"[^A-Z]", "", ud))
            if same or (mandate_like and lender_key(lender_name(uc) or c.counterparty or "x")
                        == lender_key(lender_name(ud) or d.counterparty or "y")):
                rev_cr.add(id(c))
                rev_dr.add(id(d))
                break
    return rev_cr, rev_dr


def _presentation_date(t: Txn, u: str) -> date:
    """'Chrg: ECS Return on 20260602 BAJAJ..' / 'NACH RTN .. 05042026': the date the mandate was presented."""
    m = EMBED_DATE_RE.search(u)
    if m:
        y, mo, d = (m.group(1), m.group(2), m.group(3)) if m.group(1) else (m.group(6), m.group(5), m.group(4))
        try:
            pd = date(int(y), int(mo), int(d))
            if timedelta(0) <= t.date - pd <= timedelta(days=120):
                return pd
        except ValueError:
            pass
    return t.date


def _chain_cluster(ts: list[Txn]) -> list[list[Txn]]:
    """Amount clusters: each step within 3% of the previous amount and the whole cluster within 10%
    (a varying presented EMI 22,242 .. 24,463 stays one loan; 12,083 and 22,242 never merge)."""
    cl: list[list[Txn]] = []
    for t in sorted(ts, key=lambda x: x.amount):
        if cl and t.amount <= cl[-1][-1].amount * 1.03 + 1 and t.amount <= cl[-1][0].amount * 1.10 + 1:
            cl[-1].append(t)
        else:
            cl.append([t])
    return [sorted(c, key=lambda x: x.date) for c in cl]


def _monthly(dates: list[date], mandate: bool) -> bool:
    """An instalment pattern: 2+ months, about one debit a month, consecutive debits ~a month apart
    (a mandate may skip a month when a presentation bounced)."""
    months = {month_key(d) for d in dates}
    if len(months) < 2 or len(dates) > len(months) + 1:
        return False
    ds = sorted(set(dates))
    if len(ds) == 2:  # two debits: about a month apart (KISSHT 20-Nov / 02-Dec is a rollover, not an EMI)
        return 20 <= (ds[1] - ds[0]).days <= (70 if mandate else 45)
    mi = sorted({int(m[:4]) * 12 + int(m[5:7]) for m in months})
    return statistics.median([b - a for a, b in zip(mi, mi[1:])]) <= (2 if mandate else 1)


def detect_loans(txns: list[Txn], last_month: str) -> dict:
    emis, disb, oneoff = [], [], []
    cc = {"count": 0, "total": 0.0}
    if not txns:
        return {"emis": [], "monthly_obligation": 0.0, "short_term_repayments": {"monthly": 0.0, "lenders": 0},
                "disbursals": [], "oneoff_repayments": [], "credit_card": cc,
                "distinct_lenders": [], "returned_mandates": []}
    end = max(t.date for t in txns)
    start = min(t.date for t in txns)
    n_months = (end.year - start.year) * 12 + end.month - start.month + 1  # calendar months the statement spans
    U = {id(t): norm(t.narration) for t in txns}
    # PSB's split payee is a layout of the whole statement: repaired only when 3+ narrations show it
    splice = sum(1 for u in U.values() if PSB_SPLICE_RE.search(u)) >= 3
    U = {i: " " + (PSB_SPLICE_RE.sub(r"\1", u) if splice else u) + " " for i, u in U.items()}
    rev_cr, rev_dr = _reversal_pairs(txns, U)
    # ---- 1. lender named in the narration
    names: dict[int, str] = {}
    insurer: set[int] = set()  # insurance debits: never a lender row, by name or by counterparty (step 3)
    for t in txns:
        u = U[id(t)]
        nm = lender_name(u)
        if nm and t.direction == "DR" and lender_key(nm).startswith(CARD_ISSUERS) \
                and not (EMI_WORD_RE.search(u) or "LOAN" in u):
            nm = None  # a card issuer's bill payment ('Slice Bill') is not a loan repayment
        if t.direction == "DR" and has_any(u, K.INSURANCE_WORDS) and not (EMI_WORD_RE.search(u) or "LOAN" in u):
            insurer.add(id(t))  # 'NACH/ADITYA BIRLA SUN LIFE INSURANCE': the group's insurer, not its lending arm
            nm = None
        if nm:
            names[id(t)] = nm
    # ---- 2. a credit that says it is a loan payout names its payer a lender
    def _disb_ok(t: Txn) -> bool:
        u = U[id(t)]
        if t.direction != "CR" or id(t) in rev_cr or t.amount < 100 or "bounce" in t.tags or "salary" in t.tags:
            return False
        if t.category in ("Refund / Reversal", "Interest", "Cash Deposit", "Bounce / Reversal"):
            return False
        if has_any(u, K.NOT_DISB_WORDS) or TRANSFER_RETURN_RE.search(u) or REV_CREDIT_RE.search(u):
            return False
        # money from the holder's own account is not a loan, unless the bank itself books it ('PL STF DISB')
        if "self_transfer" in t.tags and re.search(r"\b(?:NEFT|IMPS|RTGS|UPI)\b", u):
            return False
        return True

    unnamed_disb: set[int] = set()
    weight: dict[int, int] = {i: 2 for i in names}
    # the holder's own name ('PL STF DISB-<holder>': the bank's staff loan) is never the lender
    self_keys = {lender_key(t.counterparty) for t in txns if "self_transfer" in t.tags and t.counterparty}
    for t in txns:
        if id(t) not in names and _disb_ok(t) and DISB_WORD_RE.search(U[id(t)]):
            payer = _disb_payer(t)
            if payer and lender_key(payer) in self_keys:
                payer = None
            if payer:
                names[id(t)] = payer
                weight[id(t)] = 1
            else:
                unnamed_disb.add(id(t))
    # ---- 3. the same counterparty on another leg (repayment of a disbursing lender, payout of a collecting one)
    known: list[tuple[str, str]] = []
    for t in txns:
        if id(t) in names and "salary" not in t.tags:
            k = lender_key(names[id(t)])
            if len(k) >= 3 and all(k != kk for kk, _ in known):
                known.append((k, names[id(t)]))
    for t in txns:
        if id(t) in names or not t.counterparty or "self_transfer" in t.tags or re.search(r"\d", t.counterparty) \
                or id(t) in insurer:
            continue
        ck = lender_key(t.counterparty)
        if len(ck) >= 5:
            for k, nm in known:
                if _keys_same(ck, k):
                    names[id(t)] = nm
                    weight[id(t)] = 1
                    break
    # a payout whose payer field is the lender's name glued and cut by the remitter ('IMPS/P2A/../DPALLEAS/..' from
    # the lender debited as 'ACH-DR-D PAL LEASING AND FI-'): one 7-14 letter word that holds the lender's key followed
    # by the start of a finance word, and that is a run of the letters of one of its named debits. Credits only
    glued_dr: dict[str, list[str]] = defaultdict(list)
    glued_amt: dict[str, list[float]] = defaultdict(list)
    for t in txns:
        if t.direction == "DR" and id(t) in names:
            glued_dr[lender_key(names[id(t)])].append(re.sub(r"[^A-Z]", "", U[id(t)]))
            glued_amt[lender_key(names[id(t)])].append(t.amount)
    glued_cand: dict[tuple, list[Txn]] = defaultdict(list)
    for t in txns:
        if t.direction != "CR" or id(t) in names or "self_transfer" in t.tags or not t.counterparty \
                or not re.fullmatch(r"[A-Z]{7,14}", t.counterparty.strip()):
            continue
        tok = t.counterparty.strip()
        hits = {(k, nm) for k, nm in known if len(k) >= 3 and k in tok
                and re.match(r"LEAS|FIN|CAP|CRED|LOAN", tok[tok.index(k) + len(k):])
                and any(tok in g for g in glued_dr.get(k, ()))}
        # a refund of an instalment is not a payout
        if len(hits) == 1 and not any(abs(t.amount - a) <= 0.01 * a for a in glued_amt[next(iter(hits))[0]]):
            glued_cand[next(iter(hits))].append(t)
    for (k, nm), cs in glued_cand.items():
        if len({month_key(c.date) for c in cs}) >= 2:  # payouts in 2+ months (one refund / surplus is not)
            for c in cs:
                names[id(c)] = nm
                weight[id(c)] = 1
    # canonical lender per key (merge 'BAJAJ FIN' / 'BAJAJFIN' / 'BAJAJ FINANCE', 'KASAR CREDIT' / 'KASARCREDIT')
    canon: list[tuple[str, Counter]] = []

    def _canon(nm: str, w: int) -> str:
        k = lender_key(nm)
        for kk, cnt in canon:
            if _keys_same(k, kk):
                cnt[nm] += w
                return kk
        canon.append((k, Counter({nm: w})))
        return k

    lkey = {i: _canon(nm, weight.get(i, 1)) for i, nm in names.items()}

    def _display(k: str) -> str:
        for kk, cnt in canon:
            if kk == k:
                return _tidy(max(cnt.items(), key=lambda kv: (kv[1], len(kv[0])))[0])
        return k

    lenders: set[str] = set()
    disb_keys: set[str] = set()  # lenders that paid money in during the statement
    lend_dates: dict[str, list[tuple]] = defaultdict(list)  # (date, amount) of each of their payouts
    # ---- disbursals
    for t in txns:
        if t.direction != "CR":
            continue
        u = U[id(t)]
        if t.category == "Loan Disbursal" and not _disb_ok(t):
            if id(t) in rev_cr or REV_CREDIT_RE.search(u):
                t.category = "Bounce / Reversal"  # a returned EMI presentation credited back, not a new loan
            continue
        named = id(t) in names and _disb_ok(t) and (DISB_WORD_RE.search(u) or "LOAN" in u or t.amount >= 2000)
        if named or id(t) in unnamed_disb:
            if t.category != "Loan Disbursal":
                t.category = "Loan Disbursal"
                t.tags.append("lender_inferred")
            disb.append({"date": t.date.isoformat(), "lender": _display(lkey[id(t)]) if named else "(payer not named)",
                         "amount": t.amount, "narration": t.narration[:100]})
            if named:
                lenders.add(lkey[id(t)])
                disb_keys.add(lkey[id(t)])
                lend_dates[lkey[id(t)]].append((t.date, t.amount))
    # ---- repayments
    groups: dict[str, list[Txn]] = defaultdict(list)
    gname: dict[str, str] = {}
    gmandate: dict[str, bool] = defaultdict(bool)
    glender: dict[str, bool] = defaultdict(bool)
    presented: dict[str, list[date]] = defaultdict(list)
    # returns that name the mandate (UMRN) or the presented amount instead of the lender ('ECSRTN1_0305257020403252010967',
    # 'CHGS-ACHRet-310326- Amt4175.04-RN-2002- CTRAZORP'): dated by the return date printed in them
    presented_m: dict[str, list[date]] = defaultdict(list)
    presented_amt: list[tuple[float, date]] = []
    returned_ids: set[int] = set()
    tnach: set[int] = set()  # debits presented on a NACH / ACH mandate
    tmid: dict[int, str] = {}  # debit -> its mandate reference
    mid_ids: dict[str, set[int]] = defaultdict(set)  # mandate reference -> its debits
    mid_key: dict[str, str] = {}  # mandate reference -> the group its first debit went to
    part_keys: set[str] = set()  # own loan recovered in parts (PART_RECOVERY_RE)
    # CMS debits on a loan number that a NACH return charge quotes: a mandate on that loan (not otherwise named)
    rtn_ids = {m[1] for t in txns if t.direction == "DR" for m in [NACH_RTN_ID_RE.search(U[id(t)])] if m}

    def _id_hit(a: str, b: str) -> bool:
        return min(len(a), len(b)) >= 5 and (a.startswith(b) or b.startswith(a))
    cms_ids: set[str] = set()
    for t in txns:
        m = CMS_LOAN_RE.search(U[id(t)]) if t.direction == "DR" else None
        if m and any(_id_hit(m[2] + (m[3] or ""), r) for r in rtn_ids):
            cms_ids.add(m[2] + (m[3] or ""))
    # IOB 'BDECS-' debits are ECS mandate debits; taken as such only for a mandate (UMRN) debited the same amount
    # (+-1%) in 3+ months
    bd: dict[str, list[Txn]] = defaultdict(list)
    for t in txns:
        m = BDECS_RE.search(U[id(t)]) if t.direction == "DR" else None
        if m:
            bd[m[2]].append(t)
    bdecs_ok = {mid for mid, ts in bd.items()
                if any(len({month_key(x.date) for x in ts if abs(x.amount - a.amount) <= 0.01 * a.amount}) >= 3
                       for a in ts)}
    # a lender name cut short in a return charge ('~ADITYA BI'): the one named lender whose name starts with it
    named_keys: dict[str, set[str]] = defaultdict(set)
    tx_by_id = {id(t): t for t in txns}
    for i, nm in names.items():
        if tx_by_id[i].direction == "DR" and "salary" not in tx_by_id[i].tags:  # repayment side only
            named_keys[re.sub(r"[^A-Z]", "", norm(nm))].add(lkey[i])
    presented_cut: dict[str, list[date]] = defaultdict(list)  # cut-name returns, accepted only chained
    # a bank's number that changes between months at the same amount is a presentation reference
    bank_refs: dict[str, list[tuple]] = defaultdict(list)
    bank_day: dict[str, tuple] = {}  # RV2: number -> (posting day, mandate token printed after it)
    for t in txns:
        m = BANK_LOAN_ACH_RE.search(U[id(t)]) if t.direction == "DR" else None
        if m:
            bank_refs[m[1]].append((m[2], t.amount, month_key(t.date)))
            tok = re.match(r"\s*([A-Z]{4}\d{6,})", U[id(t)][m.end():])
            bank_day.setdefault(m[2], (t.date, tok[1] if tok else None))
    unstable = {n for xs in bank_refs.values() for n, a, mk in xs
                if any(n2 != n and mk2 != mk and abs(a2 - a) <= 0.03 * a for n2, a2, mk2 in xs)}
    # one debit on one number proves no loan (a bank's card autopay prints a customer number too): a stable number is
    # a loan a/c when it is debited in 2+ months, or (RV2) when another stable number of the same bank is debited the
    # SAME day under the SAME printed mandate token (one mandate batch: AXIS_003 HDFC 414097540 / 414047050)
    bank_ok: set[str] = set()
    for xs in bank_refs.values():
        stable = {n for n, _, _ in xs if n not in unstable}
        for n in stable:
            twin = any(n2 != n and bank_day[n2][1] and bank_day[n2] == bank_day[n] for n2 in stable)
            if twin or len({mk for n2, _, mk in xs if n2 == n}) >= 2:
                bank_ok.add(n)
    for t in txns:
        if t.direction != "DR":
            continue
        u = U[id(t)]
        if t.category == "Credit Card Payment":
            cc["count"] += 1
            cc["total"] += t.amount
            continue
        is_bounce = "bounce" in t.tags or "bounce_charge" in t.tags
        if is_bounce:
            rd = _return_date(u, t.date)
            pdate = rd if isinstance(rd, date) else t.date
            mid = _mandate_id(u)
            if mid:
                presented_m[mid].append(pdate)
            am = re.search(r"(?<![A-Z])AMT\s?(\d[\d,]*(?:\.\d+)?)", u)
            if am:
                # the payee printed after the amount ('.. Amt4175.04-RN-2002- CTRAZORP'), if any
                party = [w for w in re.findall(r"[A-Z]{4,}", u[am.end():]) if w not in CHANNEL_TOKENS]
                try:
                    presented_amt.append((float(am[1].replace(",", "")), pdate, party))
                except ValueError:
                    pass
        mandate = bool(MANDATE_RE.search(u))
        bdm = BDECS_RE.search(u)
        bdm = bdm if bdm and bdm[2] in bdecs_ok else None
        mandate = mandate or bool(bdm)
        upi_mandate = mandate and "UPI" in u and bool(UPI_MANDATE_RE.search(u))
        nach = mandate and not upi_mandate
        own = _own_loan(u, charge_ok=is_bounce)  # a bounce charge on a loan account proves that loan is live
        own_name = part = None
        if not own and not is_bounce:
            part = PART_RECOVERY_RE.search(u)
            bank_loan = BANK_LOAN_ACH_RE.search(u) if nach else None
            if part:
                own = part[1]
            elif bank_loan and not bank_loan[1].startswith("TP ") and bank_loan[2] in bank_ok:
                own, own_name = bank_loan[2], f"{bank_loan[1]} loan {bank_loan[2]}"
        lk = lkey.get(id(t))
        cut_ret = False
        if is_bounce and not lk and not own:
            cut = NACH_RTN_NAME_RE.search(u)
            g = re.sub(r"[^A-Z]", "", cut[1]) if cut else ""
            hits = {k for nm, ks in named_keys.items() if len(g) >= 7 and nm.startswith(g) for k in ks}
            lk = hits.pop() if len(hits) == 1 else None
            cut_ret = bool(lk)
        cms = cms_code = None
        if not lk and not own and cms_ids:
            m = None if is_bounce else CMS_LOAN_RE.search(u)
            r = NACH_RTN_ID_RE.search(u) if is_bounce else None
            if m and m[2] + (m[3] or "") in cms_ids:
                cms, cms_code = m[2] + (m[3] or ""), m[1]
            elif r:
                hit = [c for c in sorted(cms_ids) if _id_hit(c, r[1])]
                cms = hit[0] if len(hit) == 1 else None  # an id prefix of 2+ loans names neither
            nach = nach or bool(cms)
        sub = _mandate_party(u) if nach else None
        cp = t.counterparty if t.counterparty and not re.search(r"\d", t.counterparty) else ""
        if own:
            key, name = "LOANAC" + own, own_name or f"Loan a/c {own}"
        elif lk:
            key, name = lk, _display(lk)
        elif cms:
            key, name = "CMS:" + cms, f"{cms_code or 'CMS'} loan {cms}"
        elif bdm:
            key, name = "ACH:" + bdm[2], f"{bdm[1].strip()} mandate {bdm[2]}"
        elif nach:
            key = "ACH:" + (sub or lender_key(cp))
            name = sub or cp or "ACH mandate"
        else:
            key = name = None
        mid = _mandate_id(u) if nach and not own and not is_bounce else None
        if mid and mid in mid_key:  # one mandate printed under two payee names ('TP ACH ABL' / 'AXIS BANK')
            key = mid_key[mid]
        if key is None:
            continue
        # a bounced / returned presentation proves the mandate is live; a returned debit also shows its amount
        if is_bounce:
            if own and not _own_loan(u):
                # a charge row naming the loan ('LABNGXX49218-BOUNCECHGSJUN25GST'): dated by the bounce it charges
                # (a month = its last day), never by its posting - an old charge recovered late is no presentation
                if isinstance(rd, str):
                    y, mo = map(int, rd.split("-"))
                    rd = date(y + mo // 12, mo % 12 + 1, 1) - timedelta(days=1)
                if isinstance(rd, date):
                    presented[key].append(rd)
                    gname.setdefault(key, name)
                continue
            if cut_ret:
                presented_cut[key].append(_presentation_date(t, u))
                continue
            if lk or own or nach:
                presented[key].append(_presentation_date(t, u))
                gname.setdefault(key, name)
                if lk:
                    lenders.add(lk)
            continue
        if NACH_PENALTY_RE.search(u):
            continue  # a mandate's penalty charge (Airtel 'ENACH P ENALTY <UMRN>') is never a repayment
        if has_any(u, K.SUBSCRIPTION_WORDS) or DISB_WORD_RE.search(u) or OVERDUE_RE.search(u) or t.amount < 10 \
                or has_any(u, K.CHARGE_WORDS) or "MANDATE REG" in u or (INB_EMANDATE_RE.search(u) and t.amount <= 100):
            # subscriptions, the holder's own loan payouts / transfers, penal recoveries, Re 1 mandate tests,
            # bank / mandate-registration charges (PSB 'MANDATE REG CHARGE UMRN' 177, SBI 'TO TRANSFER-INB E mandate
            # <id>' 59: one e-mandate authorised in net banking, a new id each time)
            continue
        if upi_mandate and not lk:
            continue  # UPI autopay to a merchant (OTT, app plans) is not a loan
        if not (lk or own) and (has_any(u, K.INSURANCE_WORDS) or has_any(u, K.INVESTMENT_WORDS)):
            continue  # LIC / SIP mandates
        if "self_transfer" in t.tags and not (lk or own):
            continue
        if id(t) in rev_dr:
            if not (lk or own or nach):
                continue
            returned_ids.add(id(t))
            presented[key].append(t.date)
        groups[key].append(t)
        if part:
            part_keys.add(key)
        gname.setdefault(key, name)
        gmandate[key] |= nach
        if nach:
            tnach.add(id(t))
        if mid:
            tmid[id(t)] = mid
            mid_ids[mid].add(id(t))
            mid_key.setdefault(mid, key)
        glender[key] |= bool(lk or own)
        if lk:
            lenders.add(lk)
            if id(t) not in returned_ids and t.category in (
                    "Cash Withdrawal", "UPI Out", "Bills & Recharge", "Other Debit", "Transfer Out", "Card Spend"):
                t.category = "Loan / EMI"  # Indian Bank '/BRANCH : ATM SERVICE BRANCH' UPI repayments etc.
                t.tags.append("lender_inferred")
    # a mandate party that debits 4+ separate mandates on one day, on 2+ days, and also pays the holder money in
    # (Axis 'ACH-DR-TP ACH GOODSKILLSECU- 1706816810-..' x4 on 03-02 and x5 on 30-03, 'IMPS/../GOODSKIL/..'
    # credits) runs a trading / rolling account: a repeated amount among its debits is not one fixed instalment
    # a cut-name return counts only chained to the lender's paid debits (<= 45 days after the last one)
    for key, ds in presented_cut.items():
        paid = [t.date for t in groups.get(key, []) if id(t) not in returned_ids]
        last = max(paid) if paid else None
        for d in sorted(ds):
            if last is not None and 0 <= (d - last).days <= 45:
                presented[key].append(d)
                lenders.add(key)
                last = d
    emi_key_of: dict[int, str] = {}
    burst_keys: set[str] = set()
    for key, ts in groups.items():
        # a named lender has its own roll test; an aggregator key pools many merchants
        if not gmandate[key] or key.startswith("LOANAC") or glender[key] or _AGG_RE.search(key):
            continue
        per_day = Counter(t.date for t in ts)
        if sum(1 for n in per_day.values() if n >= 4) < 2:
            continue
        glued = re.sub(r"[^A-Z]", "", (gname.get(key) or "") + " " + key.split(":", 1)[-1])
        med = statistics.median(t.amount for t in ts)
        if any(c.direction == "CR" and id(c) not in rev_cr and c.counterparty and c.amount >= med
               and "bounce" not in c.tags and c.category not in ("Refund / Reversal", "Bounce / Reversal")
               and len(re.sub(r"[^A-Z]", "", c.counterparty)) >= 6
               and re.sub(r"[^A-Z]", "", c.counterparty) in glued for c in txns):
            burst_keys.add(key)
    prev = _prev_month(last_month)
    recent_cut = end - timedelta(days=40)
    emi_keys: set[str] = set()

    def _collapse(c: list[Txn]) -> list[Txn]:
        """Re-presentations of one instalment: drop returned debits in a month that has a paid one, keep one
        returned debit for a month with none paid."""
        paid_m = {month_key(t.date) for t in c if id(t) not in returned_ids}
        out, seen = [], set()
        for t in sorted(c, key=lambda x: x.date):
            mk = month_key(t.date)
            if id(t) in returned_ids:
                if mk in paid_m or mk in seen:
                    continue
                seen.add(mk)
            out.append(t)
        return out

    seen_extra: dict[int, list[date]] = {}  # cluster (id of its first debit) -> returns that filled its months

    def _is_monthly(c: list[Txn], key: str, single: bool = False) -> bool:
        if not gmandate[key] and len(c) != len({month_key(t.date) for t in c}):
            return False  # a UPI / transfer instalment is paid once a month
        dates = [t.date for t in c]
        if not gmandate[key]:
            return _monthly(dates, False)
        # one mandate amount presented every month but returned in some: the returns fill those months. Returns
        # that name its lender count when it is the lender's only mandate amount (single)
        fill = list(presented.get(key, [])) if single else []
        # returns that do not name the lender count only when they quote this cluster's own mandate reference
        # (UMRN, and no other amount was debited on that mandate) or, for a cluster of mandate debits, exactly its
        # amount ('ACHRet-310326- Amt4175.04'); with them the pattern must cover 3+ months (YES ARKA: returned
        # once, paid once, never again - not provably an instalment)
        weak_m, weak_a = [], []
        mids = {tmid.get(id(t)) for t in c}
        if len(mids) == 1 and None not in mids and mid_ids[next(iter(mids))] <= {id(t) for t in c}:
            weak_m = list(presented_m.get(next(iter(mids)), []))
        med = statistics.median(t.amount for t in c)
        if all(id(t) in tnach for t in c):
            # an amount-only return must print this mandate's own payee ('CTRAZORP', cut from 'CTRAZORPAY'): equal
            # or a prefix, 6+ letters; a bare aggregator name ('RAZORPAY') fits any aggregator mandate and is no proof
            own = set(re.findall(r"[A-Z]{4,}", (key.split(":", 1)[-1] + " " + (gname.get(key) or "")).upper()))

            def _same(w, o):
                return w == o or (min(len(w), len(o)) >= 6 and (o.startswith(w) or w.startswith(o)))

            weak_a = [d for a, d, party in presented_amt if abs(a - med) <= max(1.0, 0.005 * med)
                      and any(not _AGG_RE.search(w) and any(_same(w, o) for o in own) for w in party)]
        weak = weak_m + weak_a
        have = {month_key(d) for d in dates}

        def _months_of(ds):  # one presentation per month not paid (the last of that month), paid debits kept
            extra = {month_key(d): d for d in sorted(ds) if month_key(d) not in have}
            return dates + list(extra.values())

        ok = _monthly(_months_of(fill), True)
        if not ok and weak:
            dd = _months_of(fill + weak)
            ok = len({month_key(d) for d in dd}) >= 3 and _monthly(dd, True)
            if ok:
                fill = fill + weak
        elif ok:
            fill = fill + weak_m  # a return quoting the mandate's UMRN still proves it live; an amount-only one not
        if ok:
            seen_extra[id(c[0])] = fill
        return ok

    lent_cache: dict[str, list[tuple]] = {}

    def _lent_dates(key: str) -> list[tuple]:
        """Dates the lender of a group paid money in: its named payouts, plus loan credits ('.. CREDITT LOAN',
        'DISBU') from the mandate's party ('ACH D- RAZORPAYSOFTWAREPRIV-SAMPATISECPD' <- 'SAMPATI SECURITIES')."""
        if key not in lent_cache:
            ds = list(lend_dates.get(key, []))
            party = key[4:] if key.startswith("ACH:") else key
            # an aggregator ('RAZORPAYSOFTWAREPRIV') routes many apps' money: its payouts are not this lender's
            if len(party) >= 5 and not _AGG_RE.search(party):
                for c in txns:
                    if c.direction != "CR" or not _disb_ok(c) or not LOAN_TEXT_RE.search(U[id(c)]):
                        continue
                    ck = lkey.get(id(c)) or (lender_key(c.counterparty) if c.counterparty else "")
                    if len(ck) >= 5 and not _AGG_RE.search(ck) and _keys_same(ck, party):
                        ds.append((c.date, c.amount))
            lent_cache[key] = sorted(set(ds))
        return lent_cache[key]

    def _emi(key, ts, amount, variable=False, extra=(), roll=False):
        months = sorted({month_key(t.date) for t in ts})
        last_seen = max([t.date for t in ts] + presented.get(key, []) + list(extra))
        u = U[id(ts[-1])]
        mech = "NACH/ACH" if gmandate[key] else ("UPI" if "UPI" in u else "Transfer")
        for t in ts:
            if id(t) not in returned_ids:
                t.tags.append("emi")
        emi_keys.add(key)
        e = {"lender": gname[key], "amount": round(amount, 2), "occurrences": len(ts), "months": months,
             "first_date": ts[0].date.isoformat(), "last_date": ts[-1].date.isoformat(), "mechanism": mech,
             # live if paid / presented last month or this month, or within 45 days of the statement end
             # (a statement ending on the 1st has a one-day last month)
             "active": month_key(last_seen) >= prev or (end - last_seen).days <= 45,
             "returned": sum(1 for t in ts if id(t) in returned_ids)}
        if variable:
            e["variable"] = True
        # fixed EMI = a contractual instalment: debited by the bank on a mandate / own loan account, or the same
        # amount (+-3%) paid 3+ months running to a lender that lent nothing in this statement. Anything else to a
        # lender (amounts that move, 2 hits, a lender that also paid money in) is a short-term / payday loan being
        # repaid or rolled over - real money out, but not a fixed monthly obligation
        paid = [t.amount for t in ts if id(t) not in returned_ids] or [amount]
        steady = max(paid) <= 1.03 * min(paid)
        bank_debit = bool(gmandate[key] or key.startswith("LOANAC"))
        # a mandate lender that pays money in again between two collections of this mandate, payouts in 2+ months of
        # at most ~2 instalments each, is rolling a payday loan, not collecting a fixed EMI (IOB Sampati ~21k out /
        # 25.5k back monthly, Respo). A payout before the instalments start is the loan being repaid (IOB Chinmay); a
        # term loan's payouts are many instalments (Bajaj housing tranches, Unifinz top-up); a credit of any of the
        # lender's debit amounts is a debit refunded. A roll pays out in at least half the statement's months (IOB
        # Sampati 7 of 7, Respo 4 of 7); two top-ups beside a 7-month EMI are not a roll
        rolling = False
        if bank_debit and not key.startswith("LOANAC") and len(ts) >= 2:
            amts = [t.amount for t in groups[key]]
            lent = [(d, a) for d, a in _lent_dates(key)
                    if not any(abs(a - x) <= 0.01 * x for x in amts)]
            lent_months = len({month_key(d) for d, _ in lent})
            rolling = (lent_months >= max(2, n_months / 2)
                       and any(ts[0].date < d < ts[-1].date for d, _ in lent)
                       and statistics.median(a for _, a in lent) <= 2 * amount)
        if rolling:
            e["kind"], e["basis"] = "short_term", "lender paid money in again while collecting this mandate"
        elif roll:
            e["kind"], e["basis"] = "short_term", "lender paid out before and right after its one collection"
        elif key in burst_keys:
            e["kind"], e["basis"] = "short_term", "party collects many mandates a day and pays money in"
        elif bank_debit and not variable:
            e["kind"], e["basis"] = "fixed", "bank-debited mandate / loan account"
        elif not bank_debit and not variable and steady and len(months) >= 3 and key not in disb_keys:
            e["kind"], e["basis"] = "fixed", f"same amount {len(months)} months running"
        elif (not bank_debit and not variable and steady and key not in disb_keys
              and any(EMI_WORD_RE.search(U[id(t)]) or "BBPSLOAN" in U[id(t)].replace(" ", "") for t in ts)):
            # the payment says it is an instalment: 'NAVI FIN/.. EMI payment', a BBPS loan-repayment bill
            e["kind"], e["basis"] = "fixed", "narration says EMI / loan bill"
        else:
            e["kind"] = "short_term"
            e["basis"] = ("lender also paid money in during this statement" if key in disb_keys and not bank_debit
                          else "amount varies" if variable or not steady else f"only {len(months)} month(s) seen")
        emi_key_of[id(e)] = key
        emis.append(e)

    for key, ts in groups.items():
        if key in part_keys:
            # own loan recovered in parts: the instalment is each month's total, proven only when 3+ months total
            # the same (+-3%); otherwise no instalment amount is provable and the recoveries are listed one-off
            paid_ts = sorted((t for t in ts if id(t) not in returned_ids), key=lambda x: x.date)
            sums: dict[str, float] = defaultdict(float)
            for t in paid_ts:
                sums[month_key(t.date)] += t.amount
            # a month with 2+ recoveries each >= 40% of its total is instalments + arrears, not one EMI
            catch_up = any(sum(1 for t in paid_ts if month_key(t.date) == mk and t.amount >= 0.4 * s) >= 2
                           for mk, s in sums.items())
            if len(sums) >= 3 and max(sums.values()) <= 1.03 * min(sums.values()) and not catch_up:
                _emi(key, paid_ts, statistics.median(sums.values()))
            else:
                oneoff.extend({"date": t.date.isoformat(), "lender": gname[key], "amount": t.amount,
                               "narration": t.narration[:100]} for t in paid_ts)
            continue
        good, left = [], []
        clusters = _chain_cluster(ts)
        # the lender's returns belong to a cluster when it is the only one (or the only one of mandate debits:
        # SIB Bajaj NACH 7,771 beside UPI repayments)
        nach_cl = [c for c in clusters if any(id(t) in tnach for t in c)]
        for c in clusters:
            single = len(clusters) == 1 or (len(nach_cl) == 1 and c is nach_cl[0])
            c = _collapse(c)
            (good if _is_monthly(c, key, single=single) else left).append(c)
        left = [t for c in left for t in c]
        # an off-amount instalment of a known EMI in a month it does not cover (UPI re-payment after a bounce)
        for c in good:
            med = statistics.median(t.amount for t in c)
            extra = seen_extra.get(id(c[0]), [])
            have = {month_key(t.date) for t in c}
            for t in list(left):
                if abs(t.amount - med) <= 0.15 * med and month_key(t.date) not in have:
                    c.append(t)
                    have.add(month_key(t.date))
                    left.remove(t)
            c.sort(key=lambda x: x.date)
            _emi(key, c, med, extra=extra)
        # a growing / rolling instalment to a proven lender or mandate: the month's largest debit, 2+ months
        # (SIB Bajaj: NACH returned, UPI 8,471.50 the same day, NACH 7,771 next month; small extras are charges)
        if left and not good and (glender[key] or gmandate[key]):
            left = _collapse(left)
            top: dict[str, Txn] = {}
            for t in left:
                mk = month_key(t.date)
                if mk not in top or t.amount > top[mk].amount:
                    top[mk] = t
            inst = sorted(top.values(), key=lambda x: x.date)
            amts = [t.amount for t in inst]
            if len(inst) >= 2 and _monthly([t.date for t in inst], gmandate[key]) and max(amts) <= 2 * min(amts):
                _emi(key, inst, inst[-1].amount, variable=True)
                used = {id(t) for t in inst}
                left = [t for t in left if id(t) not in used]
        # a single mandate / EMI debit to a proven lender in the last 40 days: a live instalment (also when the
        # lender's other debits are UPI repayments: PSB Unifinz UPI 15,164 in May, ACH 17,929 in Oct)
        mand = [t for t in left if id(t) in tnach and id(t) not in returned_ids]
        # (the one mandate debit among other repayments: not when the lender paid out in 2+ months - its one mandate
        # collection is then a payday roll, not an instalment)
        if left and not good and glender[key] and (len(left) == 1 or (
                len(mand) == 1 and len({month_key(d) for d, _ in _lent_dates(key)}) < 2)):
            t = left[0] if len(left) == 1 else mand[0]
            if t.date >= recent_cut and (gmandate[key] or EMI_WORD_RE.search(U[id(t)])) and t.amount >= 500:
                # the lender paid out before this one collection and again within a month after it (KVB Unifinz:
                # 86,762 on 12-08, NACH 62,972 on 05-10, 50,274 paid out on 06-10), in at least half the statement's
                # months (the roll test of _emi), payouts of at most ~2 such debits: a payday loan rolled over, not
                # a fixed instalment. A credit of the debit's own amount is the debit refunded, not a payout.
                # (Karnataka Zed Leafin pays out in 3 of 7 months: its one NACH 5,775 stays an instalment)
                lent = [(d, a) for d, a in _lent_dates(key) if abs(a - t.amount) > 0.01 * t.amount]
                roll = (len({month_key(d) for d, _ in lent}) >= max(2, n_months / 2)
                        and any(d < t.date for d, _ in lent) and any(0 < (d - t.date).days <= 31 for d, _ in lent)
                        and statistics.median(a for _, a in lent) <= 2 * t.amount)
                _emi(key, [t], t.amount, roll=roll)
                left = [x for x in left if x is not t]
        for t in left:
            if id(t) in returned_ids:
                continue
            oneoff.append({"date": t.date.isoformat(), "lender": gname[key], "amount": t.amount,
                           "narration": t.narration[:100]})
    # an unnamed CMS loan whose instalment is also paid to a named lender (same amount +-1%, months that
    # do not overlap: the UPI re-payment of a returned CMS presentation) is that lender's loan, not a second one
    for e in list(emis):
        if not emi_key_of.get(id(e), "").startswith("CMS:"):
            continue
        if any(o is not e and not emi_key_of.get(id(o), "").startswith("CMS:")
               and abs(o["amount"] - e["amount"]) <= 0.01 * e["amount"] and not set(o["months"]) & set(e["months"])
               for o in emis):
            emis.remove(e)
    # lenders whose mandate is presented and returned with no instalment paid: obligation exists, amount unknown
    returned = []
    for key, ds in presented.items():
        if key in emi_keys or not (key in lkey.values() or key.startswith("LOANAC")):
            continue
        returned.append({"lender": gname.get(key, key), "returns": len(ds),
                         "months": sorted({month_key(d) for d in ds}), "last_date": max(ds).isoformat()})
    returned.sort(key=lambda r: -r["returns"])
    emis.sort(key=lambda e: -e["amount"])
    obligation = round(sum(e["amount"] for e in emis if e["active"] and e["kind"] == "fixed"), 2)
    st = [e for e in emis if e["active"] and e["kind"] == "short_term"]
    short_term = {"monthly": round(sum(e["amount"] for e in st), 2), "lenders": len({e["lender"] for e in st})}
    cc["total"] = round(cc["total"], 2)
    oneoff.sort(key=lambda o: o["date"])
    return {"emis": emis, "monthly_obligation": obligation, "short_term_repayments": short_term,
            "disbursals": disb, "oneoff_repayments": oneoff[:50],
            "credit_card": cc, "distinct_lenders": sorted({_display(k) for k in lenders}),
            "returned_mandates": returned}


def _next_month(mk: str) -> str:
    y, m = map(int, mk.split("-"))
    return f"{y + m // 12:04d}-{m % 12 + 1:02d}"


def _prev_month(mk: str) -> str:
    y, m = map(int, mk.split("-"))
    m -= 1
    if m == 0:
        y, m = y - 1, 12
    return f"{y:04d}-{m:02d}"


# ------------------------------------------------------------------ main
def run(txns: list[Txn], holder: str | None, period_from: date | None, period_to: date | None,
        salary_account: bool = False) -> dict:
    holder_tokens = {w for w in re.findall(r"[A-Z]{3,}", norm(holder or "")) if w not in ("MR", "MRS", "MS", "SHRI")}
    for t in txns:
        t.counterparty = counterparty(t.narration)
        t.category, tags = categorize(t)
        t.tags.extend(tags)
    tag_self_transfers(txns, holder_tokens)

    start = min(period_from or txns[0].date, txns[0].date)
    end = max(period_to or txns[-1].date, txns[-1].date)
    days = (end - start).days + 1
    months_covered = round(days / 30.44, 1)
    last_month = month_key(end)

    business = bool(holder and (COMPANY_RE.search(norm(holder)) or norm(holder).startswith("M/S")))
    # bounces first: their reversal credits are tagged 'bounce_reversal' (not income, not a disbursal)
    bounces = detect_bounces(txns, start, period_from)
    bounce_count = bounces["count"]
    bounce_months = Counter(e["date"][:7] for e in bounces["events"] if e["type"] == "bounce")
    salary = detect_salary(txns, holder_tokens, months_covered, business, salary_account)
    loans = detect_loans(txns, last_month)

    # EOD / monthly
    eod = eod_series(txns, start, end)
    by_month: dict[str, list[Txn]] = defaultdict(list)
    for t in txns:
        by_month[month_key(t.date)].append(t)
    monthly = []
    for mk in sorted({month_key(d) for d in eod} | set(by_month)):
        ts = by_month.get(mk, [])
        vals = [v for d, v in eod.items() if month_key(d) == mk]
        cr = sum(t.credit for t in ts)
        dr = sum(t.debit for t in ts)
        monthly.append({
            "month": mk, "credits_count": sum(1 for t in ts if t.direction == "CR"), "credits": round(cr, 2),
            "debits_count": sum(1 for t in ts if t.direction == "DR"), "debits": round(dr, 2),
            "net": round(cr - dr, 2),
            "opening": round(ts[0].balance - ts[0].signed, 2) if ts and ts[0].balance is not None else None,
            "closing": round(ts[-1].balance, 2) if ts and ts[-1].balance is not None else (vals[-1] if vals else None),
            "abb": round(statistics.mean(vals), 2) if vals else None,
            "min_eod": round(min(vals), 2) if vals else None, "max_eod": round(max(vals), 2) if vals else None,
            "salary": round(sum(t.amount for t in ts if "salary" in t.tags), 2),
            "emi": round(sum(t.amount for t in ts if "emi" in t.tags), 2),
            "bounces": bounce_months.get(mk, 0),
            "cash_deposits": round(sum(t.amount for t in ts if t.category == "Cash Deposit"), 2),
            "cash_withdrawals": round(sum(t.amount for t in ts if t.category == "Cash Withdrawal"), 2),
        })
    eod_vals = list(eod.values())
    abb = round(statistics.mean(eod_vals), 2) if eod_vals else None

    # categories / counterparties / top
    cat = {"CR": defaultdict(lambda: [0, 0.0]), "DR": defaultdict(lambda: [0, 0.0])}
    payers, payees = defaultdict(lambda: [0, 0.0]), defaultdict(lambda: [0, 0.0])
    spell: dict[str, Counter] = defaultdict(Counter)
    for t in txns:
        c = cat[t.direction][t.category]
        c[0] += 1
        c[1] += t.amount
        if t.counterparty:
            # one party printed two ways ('MPOKKET FINANCIAL SERVICES PRIVATE' / '.. PVT LTD', 'PSNSUPPL YCHAINSOLUT')
            # is one row: grouped on its letters, shown under its most used spelling
            bucket = payers if t.direction == "CR" else payees
            k = _ckey(t.counterparty) or t.counterparty
            bucket[k][0] += 1
            bucket[k][1] += t.amount
            spell[k][t.counterparty] += 1

    def _cat(d):
        return sorted(({"category": k, "count": v[0], "amount": round(v[1], 2)} for k, v in d.items()),
                      key=lambda x: -x["amount"])

    def _cp(d):
        def nm(k):
            return max(spell[k].items(), key=lambda kv: (kv[1], " " in kv[0], len(kv[0])))[0] if spell[k] else k
        return sorted(({"name": nm(k), "count": v[0], "amount": round(v[1], 2)} for k, v in d.items()),
                      key=lambda x: -x["amount"])[:10]

    def _top(direction):
        ts = sorted((t for t in txns if t.direction == direction), key=lambda t: -t.amount)[:10]
        return [{"date": t.date.isoformat(), "narration": t.narration[:110], "amount": t.amount,
                 "counterparty": t.counterparty, "category": t.category} for t in ts]

    total_cr = sum(t.credit for t in txns)
    total_dr = sum(t.debit for t in txns)
    cash_dep = sum(t.amount for t in txns if t.category == "Cash Deposit")
    cash = {"deposits_count": sum(1 for t in txns if t.category == "Cash Deposit"), "deposits_amount": round(cash_dep, 2),
            "withdrawals_count": sum(1 for t in txns if t.category == "Cash Withdrawal"),
            "withdrawals_amount": round(sum(t.amount for t in txns if t.category == "Cash Withdrawal"), 2)}

    # income estimate
    full_months = [m for m in monthly if m["credits_count"] > 0]
    excl = {"Loan Disbursal", "Refund / Reversal", "Bounce / Reversal"}
    # the same exclusions on both sides, or debits look 2x credits (SCB_001, DCB_001, SBM_001, HSBC_002): own-account
    # transfers, and debit+credit round trips with one reference. Credits also drop returns / refunds and loan payouts
    # (category, or 'DISB' wording when the lender is not in the dictionary).
    rt = reversal_pairs(txns)
    adj_cr = sum(t.credit for t in txns if t.category not in excl and "self_transfer" not in t.tags
                 and id(t) not in rt and not (t.direction == "CR" and K.DISBURSAL_TEXT_RE.search(norm(t.narration))))
    adj_dr = sum(t.debit for t in txns if "self_transfer" not in t.tags and id(t) not in rt)
    avg_cr = round(adj_cr / max(1.0, months_covered), 2)
    avg_dr = round(adj_dr / max(1.0, months_covered), 2)
    # income is shown only when a salary is proven; average credits are NOT income (business / pass-through
    # accounts would show lakhs per month). FOIR / eligibility are the underwriter's call - not computed here.
    if salary["detected"]:
        income, src = salary["avg_amount"], "salary"
    else:
        income, src = None, "none"
    emi = loans["monthly_obligation"]
    low_days = round(100 * sum(1 for v in eod_vals if v < 1000) / len(eod_vals), 1) if eod_vals else None
    cash_pct = round(100 * cash_dep / total_cr, 1) if total_cr else 0.0

    summary = {
        "monthly_income": round(income, 2) if income else None, "income_source": src, "avg_monthly_credits": avg_cr,
        "avg_monthly_debits": avg_dr, "abb": abb, "min_eod_balance": round(min(eod_vals), 2) if eod_vals else None,
        "max_eod_balance": round(max(eod_vals), 2) if eod_vals else None, "existing_emi": emi,
        "short_term_repayment": loans["short_term_repayments"]["monthly"],
        "bounce_count": bounce_count, "distinct_lenders": len(loans["distinct_lenders"]),
        "salary_detected": salary["detected"],
        "low_balance_days_pct": low_days, "cash_deposit_pct": cash_pct,
    }

    flags = _risk_flags(txns, summary, loans, bounces, salary, months_covered, monthly, end)
    return {
        "months_covered": months_covered, "summary": summary, "salary": salary, "loans": loans, "bounces": bounces,
        "top_credits": _top("CR"), "top_debits": _top("DR"), "monthly": monthly,
        "categories": {"credits": _cat(cat["CR"]), "debits": _cat(cat["DR"])},
        "counterparties": {"top_payers": _cp(payers), "top_payees": _cp(payees)},
        "cash": cash, "risk_flags": flags, "period": (start, end), "full_months": len(full_months),
    }


def _risk_flags(txns, s, loans, bounces, salary, months, monthly, end) -> list[dict]:
    f = []

    def add(sev, code, msg):
        f.append({"severity": sev, "code": code, "message": msg})

    if bounces["count"]:
        recent = [m for m in bounces["months_with_bounce"] if m >= month_key(end - timedelta(days=92))]
        add("high", "BOUNCES", f"{bounces['count']} cheque/NACH bounce(s); {len(recent)} month(s) with bounces in the "
                               "last 3 months.")
    if bounces.get("prior_period_returns"):
        add("medium", "PRIOR_BOUNCES", f"Return charges for {bounces['prior_period_returns']} bounce(s) dated before "
                                       "this statement were recovered in the period.")
    if bounces.get("deposit_returns_count"):
        add("medium", "DEPOSIT_RETURNS", f"{bounces['deposit_returns_count']} cheque(s) deposited by the holder came "
                                         "back unpaid.")
    recent_disb = [d for d in loans["disbursals"] if d["date"] >= (end - timedelta(days=92)).isoformat()]
    if len(recent_disb) >= 2:
        named = sorted({d["lender"] for d in recent_disb if d["lender"] != "(payer not named)"})
        add("high", "LOAN_STACKING", f"{len(recent_disb)} loan disbursals (Rs {sum(d['amount'] for d in recent_disb):,.0f}) "
                                     f"from {len(named)} lender(s) in the last 3 months ({', '.join(named[:5])}"
                                     f"{', ...' if len(named) > 5 else ''}).")
    st = loans.get("short_term_repayments") or {}
    if st.get("lenders"):
        add("high", "SHORT_TERM_LOANS", f"Rs {st['monthly']:,.0f}/month repaid to {st['lenders']} lender(s) in "
                                        "varying / rolling amounts (short-term or payday loans). Not counted in "
                                        "existing EMI - see the EMI table.")
    ret = loans.get("returned_mandates") or []
    if ret:
        add("high", "EMI_NOT_QUANTIFIED", f"{len(ret)} lender mandate(s) presented and returned with no instalment "
                                          f"paid ({', '.join(r['lender'] for r in ret[:5])}): these loans are live "
                                          "but their EMI is not printed, so existing EMI is understated.")
    if s["distinct_lenders"] >= 3:
        add("medium", "MANY_LENDERS", f"Dealing with {s['distinct_lenders']} lenders: "
                                      f"{', '.join(loans['distinct_lenders'][:6])}.")
    g = [t for t in txns if "gambling" in t.tags]
    if g:
        add("high" if sum(t.amount for t in g) > 10000 else "medium", "GAMBLING",
            f"{len(g)} gaming/betting transaction(s), total Rs {sum(t.amount for t in g):,.0f}.")
    c = [t for t in txns if "crypto" in t.tags]
    if c:
        add("low", "CRYPTO", f"{len(c)} crypto exchange transaction(s).")
    if s["cash_deposit_pct"] > 30:
        add("medium", "CASH_HEAVY", f"{s['cash_deposit_pct']}% of credits are cash deposits.")
    if s["low_balance_days_pct"] is not None and s["low_balance_days_pct"] > 50:
        add("medium", "LOW_BALANCE", f"Balance under Rs 1,000 on {s['low_balance_days_pct']}% of days.")
    if s["min_eod_balance"] is not None and s["min_eod_balance"] < 0:
        add("medium", "NEGATIVE_BALANCE", "Account went into negative (overdrawn) balance.")
    if not salary["detected"]:
        add("low", "NO_SALARY", "No salary detected.")
    if salary.get("short_history"):
        add("medium", "SALARY_SHORT_HISTORY", f"Salary seen in only {len({m['month'] for m in salary['months']})} "
                                              "months - verify with payslips")
    if months < 2.8:
        add("medium", "SHORT_PERIOD", f"Statement covers only {months} months (3-6 expected).")
    if s["abb"] is not None and s["monthly_income"] and s["abb"] < 0.1 * s["monthly_income"]:
        add("low", "LOW_ABB", "Average balance is under 10% of monthly income (money moves out immediately).")
    if salary["detected"]:
        sal_days = {t.date for t in txns if "salary" in t.tags}
        drained = 0
        for d in sal_days:
            out = sum(t.debit for t in txns if d <= t.date <= d + timedelta(days=2))
            inn = sum(t.amount for t in txns if t.date == d and "salary" in t.tags)
            if inn and out >= 0.8 * inn:
                drained += 1
        if drained and drained >= max(1, len(sal_days) // 2):
            add("medium", "SALARY_SWEPT", "Salary is mostly withdrawn/transferred within 2 days of credit.")
    big = [t for t in txns if t.direction == "CR" and s["monthly_income"] and t.amount > 3 * s["monthly_income"]
           and t.category not in ("Loan Disbursal",)]
    if big:
        add("low", "LARGE_CREDITS", f"{len(big)} credit(s) above 3x monthly income - check source.")
    order = {"high": 0, "medium": 1, "low": 2}
    f.sort(key=lambda x: order[x["severity"]])
    return f
