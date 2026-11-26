"""BSA: multipart upload, charging rules, errors. Synthetic PDFs only (built here with PyMuPDF), no real statements."""
import tempfile
from pathlib import Path

import fitz
import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.products import bsa as bsa_product

from .test_products import ORIGIN, balance, recharge, site  # noqa: F401  (site is a fixture)


def statement_pdf(password=None) -> bytes:
    doc = fitz.open()
    page = doc.new_page()
    rows = ["Sample Bank Statement", "Date Narration Debit Credit Balance",
            "01-05-2026 Opening 0.00 0.00 1000.00", "02-05-2026 UPI sample 100.00 0.00 900.00",
            "03-05-2026 Salary sample 0.00 5000.00 5900.00"]
    for i, line in enumerate(rows):
        page.insert_text((40, 60 + 16 * i), line)
    if password:
        return doc.tobytes(encryption=fitz.PDF_ENCRYPT_AES_256, user_pw=password, owner_pw=password + "x")
    return doc.tobytes()


def v1_upload(key, data, filename="s.pdf", password=None):
    files = {"file": (filename, data, "application/pdf")} if data is not None else None
    with TestClient(app) as c:
        return c.post("/v1/bsa/analyze", headers={"Authorization": f"Bearer {key}"}, files=files,
                      data={"password": password} if password else None)


def test_not_a_pdf_is_free(site):
    r = v1_upload(site.key, b"hello, not a pdf")
    assert r.status_code == 422 and r.json()["error"]["code"] == "invalid_request"
    assert balance(site) == 1_000


def test_missing_file_is_free(site):
    r = v1_upload(site.key, None)
    assert r.status_code == 422 and r.json()["error"]["code"] == "invalid_request"
    assert balance(site) == 1_000


def test_encrypted_without_password_is_refunded(site):
    recharge(2_500)
    before = balance(site)
    r = v1_upload(site.key, statement_pdf("secret1"))
    assert r.status_code == 422 and r.json()["error"]["code"] == "password_required"
    assert "id" in r.json()
    assert balance(site) == before


def test_wrong_password_is_refunded(site):
    recharge(2_500)
    before = balance(site)
    r = v1_upload(site.key, statement_pdf("secret1"), password="nope")
    assert r.status_code == 422 and r.json()["error"]["message"] == "Wrong password for this PDF."
    assert balance(site) == before


def test_right_password_is_charged(site, monkeypatch):
    recharge(2_500)
    before = balance(site)
    seen = []
    real = bsa_product.analyze_isolated

    def spy(path, **kw):
        seen.append(path)
        return real(path, **kw)

    monkeypatch.setattr(bsa_product, "analyze_isolated", spy)
    r = v1_upload(site.key, statement_pdf("secret1"), password="secret1")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["id"].startswith("call_") and "verdict" in body["authenticity"]
    assert balance(site) == before - 2_500
    assert seen and not Path(seen[0]).exists()  # temp file removed


def test_too_little_balance(site):
    assert v1_upload(site.key, statement_pdf()).status_code == 402  # ₹10 < ₹25


def test_dashboard_try(site):
    recharge(2_500)
    before = balance(site)
    r = site.post("/account/try/bsa", files={"file": ("s.pdf", statement_pdf(), "application/pdf")})
    assert r.status_code == 200, r.text
    assert "authenticity" in r.json()
    assert balance(site) == before - 2_500
    r = site.post("/account/try/bsa", files={"file": ("s.txt", b"x", "text/plain")})
    assert r.status_code == 422 and r.json()["error"]["code"] == "invalid_request"


def test_price_listed(site):
    assert site.get("/account").json()["prices"]["bsa"] == 2500


def test_no_leftover_temp_files(site, monkeypatch):
    def boom(path, **kw):
        raise RuntimeError("engine blew up")

    monkeypatch.setattr(bsa_product, "analyze_isolated", boom)
    recharge(2_500)
    before = balance(site)
    n = len(list(Path(tempfile.gettempdir()).glob("tmp*.pdf")))
    r = v1_upload(site.key, statement_pdf())
    assert r.status_code == 500 and balance(site) == before
    assert len(list(Path(tempfile.gettempdir()).glob("tmp*.pdf"))) == n


def test_excel_download(site):
    """The report the dashboard already has comes back as BSA's Excel workbook; free, needs the session."""
    import io

    from openpyxl import load_workbook

    recharge(2_500)
    report = site.post("/account/try/bsa", files={"file": ("s.pdf", statement_pdf(), "application/pdf")}).json()
    before = balance(site)
    r = site.post("/account/bsa/excel", json=report)
    assert r.status_code == 200 and r.headers["content-type"].startswith("application/vnd.openxmlformats")
    assert f'bsa-report-{report["id"]}.xlsx' in r.headers["content-disposition"]
    sheets = load_workbook(io.BytesIO(r.content)).sheetnames
    assert sheets[0] == "Summary" and "UPI Mobiles" in sheets
    assert balance(site) == before  # not charged

    upi = {"meta": {}, "upi_mobiles": {"transactions": [
        {"idx": 1, "date": "2026-06-02", "narration": "UPI/9000000001@ybl/SAMPLE", "mobile": "9000000001", "direction": "Paid", "amount": 500.0, "balance": 9500.0}]}}
    ws = load_workbook(io.BytesIO(site.post("/account/bsa/excel", json=upi).content))["UPI Mobiles"]
    assert [c.value for c in ws[1]] == ["Date", "Narration", "Mobile number", "Credit", "Debit", "Balance"]
    assert [c.value for c in ws[2]][2:] == ["9000000001", None, 500.0, 9500.0]

    assert site.post("/account/bsa/excel", json={"nope": 1}).status_code == 422
    with TestClient(app, headers={"Origin": ORIGIN["Origin"]}) as anon:
        assert anon.post("/account/bsa/excel", json=report).status_code == 401
