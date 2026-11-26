"""Report dict -> Excel workbook (bytes). Pure function of the report, no DB access.

Copied from the BSA project (bsa/web/export.py) on 2026-11-26 so the portal's Excel matches BSA's; only the
upi_contacts import path changed. Served by POST /account/bsa/excel (account.py)."""
from __future__ import annotations

import io

from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

HEAD_FILL = PatternFill("solid", fgColor="1F3A5F")
HEAD_FONT = Font(bold=True, color="FFFFFF")
TITLE_FONT = Font(bold=True, size=13)
INR_FMT = '[>=10000000]"₹"##\\,##\\,##\\,##0.00;[>=100000]"₹"##\\,##\\,##0.00;"₹"#,##0.00'


def _g(d, *keys, default=None):
    for k in keys:
        if not isinstance(d, dict):
            return default
        d = d.get(k)
    return default if d is None else d


def _table(ws, headers: list[tuple[str, str]], rows: list[dict], start_row: int = 1, money: set[str] | None = None):
    money = money or set()
    for ci, (_, label) in enumerate(headers, 1):
        c = ws.cell(row=start_row, column=ci, value=label)
        c.fill, c.font = HEAD_FILL, HEAD_FONT
    for ri, row in enumerate(rows, start_row + 1):
        for ci, (key, _) in enumerate(headers, 1):
            v = row.get(key) if isinstance(row, dict) else None
            if isinstance(v, (list, dict)):
                v = ", ".join(map(str, v)) if isinstance(v, list) else str(v)
            c = ws.cell(row=ri, column=ci, value=v)
            if key in money and isinstance(v, (int, float)):
                c.number_format = INR_FMT
    return start_row + len(rows) + 2


def _kv(ws, pairs: list[tuple[str, object]], start_row: int = 1, money: set[str] | None = None):
    money = money or set()
    for i, (k, v) in enumerate(pairs, start_row):
        ws.cell(row=i, column=1, value=k).font = Font(bold=True)
        if isinstance(v, (list, dict)):
            v = str(v)
        c = ws.cell(row=i, column=2, value=v)
        if k in money and isinstance(v, (int, float)):
            c.number_format = INR_FMT
    return start_row + len(pairs) + 1


def _autosize(ws, max_width: int = 60):
    widths: dict[int, int] = {}
    for row in ws.iter_rows():
        for c in row:
            if c.value is not None:
                widths[c.column] = max(widths.get(c.column, 0), min(len(str(c.value)), max_width))
    for col, w in widths.items():
        ws.column_dimensions[get_column_letter(col)].width = max(w + 2, 10)
    ws.freeze_panes = ws.freeze_panes or None


def _upi_mobiles(r: dict) -> dict | None:
    um = r.get("upi_mobiles")
    if um is not None:
        return um
    try:  # report saved before this section existed: rebuild it from the saved transactions
        from .upi_contacts import build
    except Exception:
        return None
    meta = r.get("meta") or {}
    return build(r.get("transactions") or [], meta.get("bank_code"), meta.get("account_number"))


def _upi_mobiles_sheet(wb, r: dict) -> None:
    """Plain list (Anuj, 2026-11-26): every UPI row with a mobile-number UPI ID, in statement order - no totals."""
    ws = wb.create_sheet("UPI Mobiles")
    um = _upi_mobiles(r)
    if um is None:
        ws.cell(row=1, column=1, value="Not available for this report - analyse the statement again.")
        _autosize(ws)
        return
    rows = [{"date": t.get("date"), "narration": t.get("narration"), "mobile": t.get("mobile"),
             "credit": t.get("amount") if t.get("direction") == "Received" else None,
             "debit": t.get("amount") if t.get("direction") == "Paid" else None, "balance": t.get("balance")}
            for t in sorted(um.get("transactions") or [], key=lambda t: (t.get("idx") is None, t.get("idx") or 0))]
    _table(ws, [("date", "Date"), ("narration", "Narration"), ("mobile", "Mobile number"), ("credit", "Credit"),
                ("debit", "Debit"), ("balance", "Balance")], rows, 1, {"credit", "debit", "balance"})
    ws.freeze_panes = "A2"
    if rows:
        ws.auto_filter.ref = f"A1:F{len(rows) + 1}"
    _autosize(ws)


def report_to_xlsx(r: dict) -> bytes:
    wb = Workbook()
    meta, summ = r.get("meta") or {}, r.get("summary") or {}
    auth, recon = r.get("authenticity") or {}, r.get("reconciliation") or {}

    ws = wb.active
    ws.title = "Summary"
    ws.cell(row=1, column=1, value="Bank Statement Analysis").font = TITLE_FONT
    row = _kv(ws, [
        ("File", meta.get("file_name")), ("Bank", meta.get("bank_name")), ("Account holder", meta.get("account_holder")),
        ("Account number", meta.get("account_number")), ("IFSC", meta.get("ifsc")),
        ("Period from", meta.get("period_from")), ("Period to", meta.get("period_to")),
        ("Months covered", meta.get("months_covered")), ("Transactions", meta.get("txn_count")),
        ("Authenticity verdict", auth.get("verdict")), ("Authenticity score", auth.get("score")),
        ("Data match", recon.get("status")), ("Rows verified %", recon.get("verified_pct")),
        ("Underwriting data quality", (r.get("data_quality") or {}).get("level")),
        ("Data quality note", (r.get("data_quality") or {}).get("message")),
    ], 3)
    money = {"Monthly income", "Avg monthly credits", "Avg monthly debits", "ABB", "Min EOD balance",
             "Existing EMI / month", "Short-term loan repayments / month"}
    row = _kv(ws, [
        ("Monthly income", summ.get("monthly_income") if summ.get("income_source") == "salary" else "No salary detected"),
        ("Income source", "salary" if summ.get("income_source") == "salary" else "No salary detected"),
        ("Avg monthly credits", summ.get("avg_monthly_credits")), ("Avg monthly debits", summ.get("avg_monthly_debits")),
        ("ABB", summ.get("abb")), ("Min EOD balance", summ.get("min_eod_balance")),
        ("Existing EMI / month", summ.get("existing_emi")),
        ("Short-term loan repayments / month", summ.get("short_term_repayment") or 0),
        ("Bounces", summ.get("bounce_count")), ("Distinct lenders", summ.get("distinct_lenders")),
        ("Salary detected", summ.get("salary_detected")),
        ("Low balance days %", summ.get("low_balance_days_pct")), ("Cash deposit %", summ.get("cash_deposit_pct")),
    ], row + 1, money)
    ws.cell(row=row, column=1, value="Risk flags").font = TITLE_FONT
    _table(ws, [("severity", "Severity"), ("code", "Code"), ("message", "Message")], r.get("risk_flags") or [], row + 1)
    _autosize(ws)

    ws = wb.create_sheet("Authenticity")
    pm = auth.get("pdf_metadata") or {}
    row = _kv(ws, [("Verdict", auth.get("verdict")), ("Score", auth.get("score"))] +
              [(f"PDF {k}", v) for k, v in pm.items()])
    _table(ws, [("name", "Check"), ("status", "Status"), ("detail", "Detail")], auth.get("checks") or [], row + 1)
    _autosize(ws)

    ws = wb.create_sheet("Reconciliation")
    nm = recon.get("name_match") or {}
    mny = {"Opening balance", "Closing balance", "Total credits", "Total debits", "Computed closing"}
    row = _kv(ws, [
        ("Status", recon.get("status")), ("Opening balance", recon.get("opening_balance")),
        ("Closing balance", recon.get("closing_balance")), ("Total credits", recon.get("total_credits")),
        ("Total debits", recon.get("total_debits")), ("Computed closing", recon.get("computed_closing")),
        ("Closing matches", recon.get("closing_match")), ("Rows total", recon.get("rows_total")),
        ("Rows verified", recon.get("rows_verified")), ("Rows mismatched", recon.get("rows_mismatch")),
        ("Verified %", recon.get("verified_pct")), ("Printed summary match", recon.get("printed_summary_match")),
        ("Applicant name", nm.get("applicant")), ("Holder name", nm.get("holder")), ("Name match", nm.get("status")),
    ], 1, mny)
    _table(ws, [("idx", "#"), ("date", "Date"), ("narration", "Narration"), ("amount", "Amount"),
                ("balance", "Balance"), ("expected_balance", "Expected balance")],
           recon.get("mismatch_rows") or [], row + 1, {"amount", "balance", "expected_balance"})
    _autosize(ws)

    ws = wb.create_sheet("Monthly")
    _table(ws, [("month", "Month"), ("credits_count", "# Cr"), ("credits", "Credits"), ("debits_count", "# Dr"),
                ("debits", "Debits"), ("net", "Net"), ("opening", "Opening"), ("closing", "Closing"), ("abb", "ABB"),
                ("min_eod", "Min EOD"), ("max_eod", "Max EOD"), ("salary", "Salary"), ("emi", "EMI"),
                ("bounces", "Bounces"), ("cash_deposits", "Cash dep"), ("cash_withdrawals", "Cash wdl")],
           r.get("monthly") or [], 1,
           {"credits", "debits", "net", "opening", "closing", "abb", "min_eod", "max_eod", "salary", "emi",
            "cash_deposits", "cash_withdrawals"})
    ws.freeze_panes = "B2"
    _autosize(ws)

    ws = wb.create_sheet("Salary")
    sal = r.get("salary") or {}
    row = _kv(ws, [("Detected", sal.get("detected")), ("Confidence", sal.get("confidence")),
                   ("Employer", sal.get("employer")), ("Average amount", sal.get("avg_amount")),
                   ("Usual credit day", sal.get("usual_day"))], 1, {"Average amount"})
    row = _table(ws, [("month", "Month"), ("date", "Date"), ("amount", "Amount"), ("narration", "Narration")],
                 sal.get("months") or [], row + 1, {"amount"})
    _table(ws, [("name", "Candidate"), ("score", "Score"), ("months", "Months"), ("avg_amount", "Avg amount")],
           sal.get("candidates") or [], row, {"avg_amount"})
    _autosize(ws)

    ws = wb.create_sheet("Loans-EMI")
    loans = r.get("loans") or {}
    cc = loans.get("credit_card") or {}
    st = loans.get("short_term_repayments") or {}
    row = _kv(ws, [("Fixed EMI obligation / month", loans.get("monthly_obligation")),
                   ("Short-term loan repayments / month", st.get("monthly") or 0),
                   ("Short-term lenders", st.get("lenders") or 0),
                   ("Distinct lenders", ", ".join(loans.get("distinct_lenders") or [])),
                   ("Credit card payments (count)", cc.get("count")), ("Credit card payments (total)", cc.get("total"))],
              1, {"Fixed EMI obligation / month", "Short-term loan repayments / month", "Credit card payments (total)"})
    ws.cell(row=row, column=1, value="EMIs").font = TITLE_FONT
    row = _table(ws, [("lender", "Lender"), ("kind", "Type"), ("basis", "Why"), ("amount", "EMI"), ("occurrences", "Times"), ("months", "Months"),
                      ("first_date", "First"), ("last_date", "Last"), ("mechanism", "Mechanism"), ("active", "Active")],
                 loans.get("emis") or [], row + 1, {"amount"})
    ws.cell(row=row, column=1, value="Loan disbursals").font = TITLE_FONT
    row = _table(ws, [("date", "Date"), ("lender", "Lender"), ("amount", "Amount"), ("narration", "Narration")],
                 loans.get("disbursals") or [], row + 1, {"amount"})
    ws.cell(row=row, column=1, value="One-off repayments").font = TITLE_FONT
    _table(ws, [("date", "Date"), ("lender", "Lender"), ("amount", "Amount"), ("narration", "Narration")],
           loans.get("oneoff_repayments") or [], row + 1, {"amount"})
    _autosize(ws)

    ws = wb.create_sheet("Bounces")
    b = r.get("bounces") or {}
    row = _kv(ws, [("Bounces", b.get("count")), ("Bounce charges", b.get("charges_count")),
                   ("Months with bounce", ", ".join(b.get("months_with_bounce") or []))])
    _table(ws, [("date", "Date"), ("type", "Type"), ("amount", "Amount"), ("narration", "Narration")],
           b.get("events") or [], row + 1, {"amount"})
    _autosize(ws)

    ws = wb.create_sheet("Top Txns")
    cols = [("date", "Date"), ("amount", "Amount"), ("counterparty", "Counterparty"), ("category", "Category"),
            ("narration", "Narration")]
    ws.cell(row=1, column=1, value="Top 10 credits").font = TITLE_FONT
    row = _table(ws, cols, r.get("top_credits") or [], 2, {"amount"})
    ws.cell(row=row, column=1, value="Top 10 debits").font = TITLE_FONT
    row = _table(ws, cols, r.get("top_debits") or [], row + 1, {"amount"})
    cp = r.get("counterparties") or {}
    ws.cell(row=row, column=1, value="Top payers").font = TITLE_FONT
    row = _table(ws, [("name", "Name"), ("count", "Count"), ("amount", "Amount")], cp.get("top_payers") or [], row + 1,
                 {"amount"})
    ws.cell(row=row, column=1, value="Top payees").font = TITLE_FONT
    _table(ws, [("name", "Name"), ("count", "Count"), ("amount", "Amount")], cp.get("top_payees") or [], row + 1,
           {"amount"})
    _autosize(ws)

    _upi_mobiles_sheet(wb, r)

    ws = wb.create_sheet("Transactions")
    _table(ws, [("idx", "#"), ("date", "Date"), ("value_date", "Value date"), ("narration", "Narration"),
                ("debit", "Debit"), ("credit", "Credit"), ("balance", "Balance"), ("category", "Category"),
                ("counterparty", "Counterparty"), ("verified", "Verified"), ("tags", "Tags")],
           r.get("transactions") or [], 1, {"debit", "credit", "balance"})
    ws.freeze_panes = "A2"
    ws.auto_filter.ref = f"A1:K{max(len(r.get('transactions') or []) + 1, 2)}"
    _autosize(ws)
    for row_cells in ws.iter_rows(min_row=2, min_col=4, max_col=4):
        for c in row_cells:
            c.alignment = Alignment(wrap_text=False)

    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()
