"""Payslip Checker: multipart upload, charging rules, errors, public output. Synthetic PDFs only (PyMuPDF), no network."""
import json
import re
import threading

import fitz
import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.payslip import check as engine
from app.products import payslip as payslip_product

from .test_products import balance, recharge, site  # noqa: F401  (site is a fixture)

VERDICTS = {"GENUINE", "NEEDS_REVIEW", "PROBABLY_FAKE", "FAKE"}
LEAK = re.compile(r"gemini|google|\bAI\b|model", re.I)
PUBLIC_KEYS = {"verdict", "risk", "confidence", "summary", "red_flags", "looks_fine", "unlocked", "checks", "ids", "payslips", "metadata"}


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def boom(*a, **kw):
        raise AssertionError("the AI step must not run in tests")

    monkeypatch.setattr(engine, "_gemini", boom)
    monkeypatch.setattr(engine, "GEMINI_API_KEY", "")


def pdf_of(lines, password=None) -> bytes:
    doc = fitz.open()
    page = doc.new_page()
    for i, line in enumerate(lines):
        page.insert_text((40, 60 + 16 * i), line)
    if password:
        return doc.tobytes(encryption=fitz.PDF_ENCRYPT_AES_256, user_pw=password, owner_pw=password + "x")
    return doc.tobytes()


PAYSLIP = ["SAMPLE COMPANY LTD", "Salary Slip for May 2026", "Employee Name: Test Person   Employee Code: T001",
           "Earnings   Amount", "Basic   30000", "HRA   12000", "Special Allowance   8000", "Gross Earnings   50000",
           "Deductions   Amount", "Provident Fund   1800", "Professional Tax   200", "Total Deductions   2000",
           "Net Pay   48000", "Net Pay in words: Forty Eight Thousand Only"]
STATEMENT = ["Sample Bank Statement", "Opening Balance 1000.00", "Closing Balance 900.00", "Date Narration Withdrawals Deposits"]


def post(key, data, password=None, url="/v1/payslip/check"):
    files = {"file": ("p.pdf", data, "application/pdf")} if data is not None else None
    with TestClient(app) as c:
        return c.post(url, headers={"Authorization": f"Bearer {key}"}, files=files, data={"password": password} if password else None)


def test_not_a_pdf_is_free(site):
    r = post(site.key, b"hello, not a pdf")
    assert r.status_code == 422 and r.json()["error"]["code"] == "invalid_request"
    assert balance(site) == 1_000
    assert post(site.key, None).status_code == 422


def test_encrypted_without_password_is_refunded(site):
    recharge(1_000)
    before = balance(site)
    r = post(site.key, pdf_of(PAYSLIP, "secret1"))
    assert r.status_code == 422 and r.json()["error"]["code"] == "password_required" and "id" in r.json()
    assert r.json()["error"]["message"] == "This payslip is password protected. Send its password."
    assert balance(site) == before
    r = post(site.key, pdf_of(PAYSLIP, "secret1"), password="nope")
    assert r.json()["error"]["message"] == "Wrong password for this payslip."
    assert balance(site) == before


def test_not_a_payslip_is_refunded(site):
    recharge(1_000)
    before = balance(site)
    r = post(site.key, pdf_of(STATEMENT))
    assert r.status_code == 422 and r.json()["error"]["code"] == "not_payslip"
    assert r.json()["error"]["message"] == "This is not a payslip. It looks like a bank statement."
    assert not LEAK.search(r.text)
    assert balance(site) == before


def test_payslip_is_charged_and_public_output_is_clean(site):
    recharge(1_000)
    before = balance(site)
    r = post(site.key, pdf_of(PAYSLIP))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["id"].startswith("call_") and body["verdict"] in VERDICTS
    assert set(body) == {"id", *PUBLIC_KEYS}
    assert body["risk"] == payslip_product.RISK[body["verdict"]] and body["metadata"]["pages"] == 1
    assert all(set(c) == {"label", "status", "detail"} for c in body["checks"]) and body["checks"]
    assert not LEAK.search(json.dumps(body))
    assert balance(site) == before - 1_000


def test_right_password_works(site):
    recharge(1_000)
    r = post(site.key, pdf_of(PAYSLIP, "secret1"), password="secret1")
    assert r.status_code == 200 and r.json()["verdict"] in VERDICTS


def test_public_strips_ai_wording():
    out = payslip_product.public({
        "verdict": "PROBABLY_FAKE", "payslips": [], "ids": {}, "mode": "text", "sent_to_ai": "x", "ai_error": None,
        "ai": {"verdict": "PROBABLY_FAKE", "confidence": "HIGH", "summary": "Totals match.", "model": "gemini-x",
               "red_flags": ["HRA is 80 percent of basic", "The AI saw a pasted logo"], "positives": ["Saved once"]},
        "unlocked": {"tool": "iLovePDF", "filename_hint": False, "printed": False},
        "metadata": {"producer": "Skia/PDF Google Docs Renderer", "xmp_tools": ["Google Sheets"], "pages": 2, "has_text": True},
        "checks": [
            {"id": "doc", "label": "Document type", "status": "pass", "detail": "Payslip (confirmed by AI — the layout is unusual)."},
            {"id": "x", "label": "Software", "status": "fail", "detail": "Made with Google Docs."},
            {"id": "y", "label": "AI review", "status": "pass", "detail": "ok"},
        ]})
    assert set(out) == PUBLIC_KEYS
    assert [c["detail"] for c in out["checks"]] == ["Payslip.", "Made with an online editor."]
    assert (out["risk"], out["confidence"], out["summary"]) == ("HIGH", "HIGH", "Totals match.")
    assert out["red_flags"] == ["HRA is 80 percent of basic"] and out["looks_fine"] == ["Saved once"]
    assert out["unlocked"] is True and out["metadata"]["pages"] == 2 and "has_text" not in out["metadata"]
    assert not LEAK.search(json.dumps(out))


def test_dashboard_try(site):
    recharge(1_000)
    before = balance(site)
    r = site.post("/account/try/payslip", files={"file": ("p.pdf", pdf_of(PAYSLIP), "application/pdf")})
    assert r.status_code == 200 and r.json()["verdict"] in VERDICTS
    assert balance(site) == before - 1_000
    assert site.post("/account/try/payslip", files={"file": ("p.pdf", b"nope", "application/pdf")}).status_code == 422
    assert site.get("/account").json()["prices"]["payslip"] == 1_000


def test_no_json_route_for_payslip(site):
    with TestClient(app) as c:
        r = c.post("/v1/payslip/check", headers={"Authorization": f"Bearer {site.key}"}, json={"pdf": "x"})
    assert r.status_code == 422 and r.json()["error"]["code"] == "invalid_request"  # multipart route: no file sent


def test_busy_is_free(site, monkeypatch):
    recharge(2_000)
    gate, started = threading.Event(), threading.Event()

    def slow(*a, **kw):
        started.set()
        gate.wait(10)
        return {"verdict": "GENUINE", "payslips": [], "ids": {}, "checks": []}

    monkeypatch.setattr(payslip_product, "check_payslip", slow)
    monkeypatch.setattr(payslip_product.LIMIT, "_slots", threading.BoundedSemaphore(1))
    monkeypatch.setattr(payslip_product.LIMIT, "wait_s", 0.3)
    results = {}
    t = threading.Thread(target=lambda: results.update(first=post(site.key, pdf_of(PAYSLIP))))
    t.start()
    assert started.wait(10)
    before = balance(site)
    r = post(site.key, pdf_of(PAYSLIP))
    assert r.status_code == 429 and r.json()["error"]["code"] == "busy" and r.headers["retry-after"] == "30"
    assert balance(site) == before
    gate.set()
    t.join()
    assert results["first"].status_code == 200


def test_damaged_pdf_is_refunded_with_a_clear_error(site):
    recharge(1_000)
    before = balance(site)
    with TestClient(app) as c:
        r = c.post("/v1/payslip/check", headers={"Authorization": f"Bearer {site.key}"},
                   files={"file": ("p.pdf", b"%PDF-1.7\nnot really a pdf", "application/pdf")})
    assert r.status_code == 422 and r.json()["error"]["code"] == "unsupported_file"
    assert balance(site) == before
