"""UPI payments to / from a mobile-number UPI ID ('9785943365@ybl'): one row per phone number, with every
transaction behind it. Works on report transaction dicts (idx, date, narration, debit, credit, balance,
counterparty, category, verified, tags), so the Excel export can rebuild it for reports saved before it existed.

Rules (surveyed on the 146 sample files, 2026-11-26):
- handle: the UPI ID's name part IS the mobile - 10 digits starting 6-9 (optional 91 prefix, PhonePe '-2'
  suffix, '.wa'-style suffix), then '@'. No letter / digit / dot glued in front ('Q978594336@ybl' merchant code,
  12-digit RRN, 'paytm.9785..' are not phones). An account-number UPI ID ('<a/c>@<IFSC>.ifsc.npci') and an
  e-mail ('..@gmail') are not phones.
- wrapped: a handle broken by a line wrap ('9785 943365@ybl', 10 digits in two pieces). All 87 such joins were
  phones in Indian Bank / RBL / ICICI; in TJSB only 63% - elsewhere a wrap is taken only when the same number is
  also printed whole in the file.
- SBI prints the UPI ID without '@psp': 'UPI/DR/<rrn>/<name>/<bank code>/9785943365/<remark>' - the field after
  the 4-letter bank code (181 of 181 such 10-digit fields start 6-9). Bank-gated: the same slot in AU / PSB /
  TBSB / CUB / Ujjivan holds refs or account numbers (26-90% start 6-9).
- 'From:<vpa> To:<vpa>' (YES Bank) names both sides: a debit's counterparty is after 'To:', a credit's after
  'From:' (the other side is the customer's own UPI ID).
- the customer's own number (account number = mobile, Airtel / Paytm) is never a counterparty."""
from __future__ import annotations

import re
from collections import Counter, defaultdict

_HANDLE_RE = re.compile(r"(?<![A-Za-z0-9.])(?:91)?([6-9]\d{9})(?:\s?-\s?\d{1,2}|(?:\.[A-Za-z][A-Za-z0-9]{0,9}){1,2})?"
                        r"\s?@\s?([A-Za-z][A-Za-z0-9.]{0,30})?")
_WRAPPED_RE = re.compile(r"(?<![A-Za-z0-9.])(\d{1,9}) (\d{1,9})(?:\s?-\s?\d{1,2})?\s?@\s?([A-Za-z][A-Za-z0-9.]{0,30})?")
_SBI_SLOT_RE = re.compile(r"/\s?[A-Z]{4}\s?/\s?([6-9]\d{9})(?=\s?/|\s|$)")
_UPI_RE = re.compile(r"(?<![A-Z])UPI|\b[A-Z]{4}0[A-Z0-9]{2,6}UPI")
_LABEL_RE = re.compile(r"(?<![A-Za-z])(FROM|TO)\s?:", re.I)
_IFSC_RE = re.compile(r"[A-Za-z]{4}0[A-Za-z0-9]{6}")
_EMAIL_DOMAINS = {"gmail", "yahoo", "ymail", "hotmail", "outlook", "live", "rediffmail", "icloud", "rocketmail",
                  "aol", "protonmail", "zoho"}
WRAP_OK_BANKS = {"INDIAN", "RBL", "ICICI"}  # every wrapped join in their samples was a phone


def _psp(raw: str | None) -> str | None:
    """'ybl' for a real UPI app suffix; None when the '@' part shows it is not a mobile UPI ID."""
    p = (raw or "").strip(".")
    if not p:
        return ""  # cut after '@' by the bank
    first = p.split(".")[0]
    if _IFSC_RE.fullmatch(first) or "npci" in p.lower() or first.lower() in _EMAIL_DOMAINS:
        return None
    return first.lower()


def _counterparty_part(narr: str, is_debit: bool) -> str:
    """With 'From:' and 'To:' labels, only the counterparty's side; otherwise the whole narration."""
    labels = list(_LABEL_RE.finditer(narr))
    if {m.group(1).upper() for m in labels} != {"FROM", "TO"}:
        return narr
    want = "TO" if is_debit else "FROM"
    return " ".join(narr[m.end():(labels[i + 1].start() if i + 1 < len(labels) else len(narr))]
                    for i, m in enumerate(labels) if m.group(1).upper() == want)


def _own_mobile(account_number: str | None) -> str | None:
    digits = "".join(ch for ch in str(account_number or "") if ch.isdigit())
    if len(digits) == 12 and digits.startswith("91"):
        digits = digits[2:]
    return digits if len(digits) == 10 and digits[0] in "6789" else None


def row_mobiles(narration: str, is_debit: bool, bank_code: str | None = None) -> list[tuple[str, str, str]]:
    """[(mobile, psp, how)] in a narration; how = handle | wrapped | sbi_slot (wrapped not yet confirmed)."""
    if not narration:
        return []
    part = _counterparty_part(narration, is_debit)
    out: list[tuple[str, str, str]] = []
    if "@" in part:
        for m in _HANDLE_RE.finditer(part):
            psp = _psp(m.group(2))
            if psp is not None:
                out.append((m.group(1), psp, "handle"))
        if not out:
            for m in _WRAPPED_RE.finditer(part):
                mob = m.group(1) + m.group(2)
                psp = _psp(m.group(3))
                if len(mob) == 10 and mob[0] in "6789" and psp is not None:
                    out.append((mob, psp, "wrapped"))
    elif bank_code == "SBI" and _UPI_RE.search(part.upper()):
        out.extend((m.group(1), "", "sbi_slot") for m in _SBI_SLOT_RE.finditer(part))
    return out


def _note(t: dict) -> str:
    tags = set(t.get("tags") or [])
    notes = []
    if "self_transfer" in tags:
        notes.append("own account")
    if (t.get("category") or "") in ("Refund / Reversal", "Bounce / Reversal") or "bounce_reversal" in tags:
        notes.append("reversal")
    if not t.get("verified"):
        notes.append("not verified by balance")
    return ", ".join(notes)


_HOW = {"handle": "UPI ID", "wrapped": "UPI ID (wrapped line)", "sbi_slot": "UPI ID (bank printed no @app)"}


def empty() -> dict:
    return {"count": 0, "txn_count": 0, "paid_count": 0, "paid_total": 0.0, "received_count": 0,
            "received_total": 0.0, "contacts": [], "transactions": []}


def build(transactions: list[dict], bank_code: str | None = None, account_number: str | None = None) -> dict:
    own = _own_mobile(account_number)
    found = []
    for t in transactions or []:
        debit, credit = float(t.get("debit") or 0), float(t.get("credit") or 0)
        if debit <= 0 and credit <= 0:
            continue
        hits = [h for h in row_mobiles(t.get("narration") or "", debit > 0, bank_code) if h[0] != own]
        if hits:
            found.append((t, debit, credit, hits))
    whole = {mob for _, _, _, hits in found for mob, _, how in hits if how == "handle"}

    rows, by_mob = [], defaultdict(list)
    for t, debit, credit, hits in found:
        hits = [h for h in hits if h[2] != "wrapped" or bank_code in WRAP_OK_BANKS or h[0] in whole]
        if not hits:
            continue
        # a narration naming two phones (sender and receiver) is rare; the row goes under the first one
        mob, psp, how = hits[0]
        row = {"idx": t.get("idx"), "date": t.get("date"), "mobile": mob,
               "upi_id": f"{mob}@{psp}" if psp else mob, "source": _HOW[how],
               "direction": "Paid" if debit > 0 else "Received", "amount": round(debit if debit > 0 else credit, 2),
               "balance": t.get("balance"), "name": t.get("counterparty") or "",
               "narration": t.get("narration") or "", "verified": bool(t.get("verified")), "note": _note(t)}
        rows.append(row)
        by_mob[mob].append(row)

    contacts = []
    for mob, rs in by_mob.items():
        paid = [r for r in rs if r["direction"] == "Paid"]
        recd = [r for r in rs if r["direction"] == "Received"]
        names = Counter(r["name"] for r in rs if r["name"])
        apps = sorted({r["upi_id"].split("@", 1)[1] for r in rs if "@" in r["upi_id"]})
        dates = sorted(r["date"] for r in rs if r["date"])
        p_amt, r_amt = round(sum(r["amount"] for r in paid), 2), round(sum(r["amount"] for r in recd), 2)
        contacts.append({
            "mobile": mob, "name": names.most_common(1)[0][0] if names else "", "upi_apps": ", ".join(apps),
            "paid_count": len(paid), "paid": p_amt, "received_count": len(recd), "received": r_amt,
            "net": round(r_amt - p_amt, 2), "txn_count": len(rs),
            "first_date": dates[0] if dates else None, "last_date": dates[-1] if dates else None,
            "months": len({d[:7] for d in dates}),
            "unverified": sum(1 for r in rs if not r["verified"]),
            "own_account": any("own account" in r["note"] for r in rs),
        })
    contacts.sort(key=lambda c: (-(c["paid"] + c["received"]), c["mobile"]))
    rank = {c["mobile"]: i for i, c in enumerate(contacts)}
    rows.sort(key=lambda r: (rank[r["mobile"]], r["date"] or "", r["idx"] if r["idx"] is not None else 0))
    out = empty()
    out.update({
        "count": len(contacts), "txn_count": len(rows),
        "paid_count": sum(c["paid_count"] for c in contacts),
        "paid_total": round(sum(c["paid"] for c in contacts), 2),
        "received_count": sum(c["received_count"] for c in contacts),
        "received_total": round(sum(c["received"] for c in contacts), 2),
        "contacts": contacts, "transactions": rows,
    })
    return out
