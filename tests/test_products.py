"""Product APIs: PAN, GSTIN, IFSC, EMI, the charging rules around them, and the dashboard's Try."""
import io
from datetime import date

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, select

from app.db import SessionLocal
from app.main import app
from app.models import Admin, ApiCall, ApiKey, Client, LoginSession, Wallet, WalletEntry
from app.products import gstin, ifsc, pan
from app.security import hash_password

ORIGIN = {"Origin": "http://site.test"}
PW = "correct horse 1"
IFSC_CSV = """BANK,IFSC,BRANCH,CENTRE,DISTRICT,STATE,ADDRESS,CONTACT,IMPS,RTGS,CITY,ISO3166,NEFT,MICR,UPI,SWIFT
State Bank of India,SBIN0000691,NEW DELHI MAIN BRANCH,NEW DELHI,NEW DELHI,DELHI,"11 SANSAD MARG, NEW DELHI 110 001",,true,true,NEW DELHI,IN-DL,true,110002087,true,SBININBB104
,AANB0000001,RTGS-HO,SURAT,SURAT,GUJARAT,"KATARGAM, SURAT",,true,true,SURAT,IN-GJ,true,,false,
"""


@pytest.fixture
def site():
    """Logged-in client 'Asha' with an API key, ₹10 balance and IFSC data loaded."""
    with TestClient(app, headers=ORIGIN) as c:
        with SessionLocal() as db:
            for m in (ApiCall, ApiKey, WalletEntry, Wallet, LoginSession, Admin, Client):
                db.execute(delete(m))
            db.add(Client(email="asha@example.com", name="Asha", password_hash=hash_password(PW)))
            db.commit()
            ifsc.load(db, io.StringIO(IFSC_CSV), "v-test", date(2026, 9, 1))
        assert c.post("/auth/login", json={"email": "asha@example.com", "password": PW}).status_code == 200
        c.key = c.post("/account/keys").json()["key"]
        recharge(START)
        yield c


def v1(method, path, key, body=None):
    with TestClient(app) as c:  # server-to-server: no cookie, no Origin
        return c.request(method, f"/v1{path}", headers={"Authorization": f"Bearer {key}"}, json=body)


START = 1_000  # ₹10


def balance(site):
    return site.get("/account").json()["balance_paise"]


def empty_wallet():
    with SessionLocal() as db:
        db.execute(Wallet.__table__.update().values(balance_paise=0))
        db.commit()


def recharge(paise):
    from app import wallet
    with SessionLocal() as db:
        wallet.credit(db, db.scalar(select(Client.id)), paise, "recharge", "UPI UTR 1")
        db.commit()


# --- PAN
@pytest.mark.parametrize("code,kind", list(pan.HOLDER_TYPES.items()))
def test_pan_holder_types(code, kind):
    assert pan.check(f"ABC{code}E1234F") == {"pan": f"ABC{code}E1234F", "format_valid": True, "reason": None, "holder_type": kind}


@pytest.mark.parametrize("raw,reason", [("ABCPE1234", "length"), ("ABCPE12345", "format"), ("1BCPE1234F", "format"), ("ABCXE1234F", "holder_type")])
def test_pan_invalid(raw, reason):
    assert pan.check(raw) | {"pan": None} == {"pan": None, "format_valid": False, "reason": reason, "holder_type": None}


def test_pan_normalises():
    assert pan.check("  abcpe1234f ")["pan"] == "ABCPE1234F"


# --- GSTIN
def test_gstin_valid():
    assert gstin.check(" 27aapfu0939f1zv ") == {"gstin": "27AAPFU0939F1ZV", "format_valid": True, "reason": None, "state": "Maharashtra", "pan": "AAPFU0939F"}


@pytest.mark.parametrize("raw,reason", [
    ("27AAPFU0939F1Z", "length"),
    ("27AAPFU0939F1AV", "format"),  # 14th character must be Z
    ("40AAPFU0939F1ZW", "state_code"),
    ("27AAPFU0939F1ZX", "check_digit"),
])
def test_gstin_invalid(raw, reason):
    r = gstin.check(raw)
    assert (r["format_valid"], r["reason"], r["state"], r["pan"]) == (False, reason, None, None)


# --- Over HTTP: charging rules
def test_pan_call_charges_the_balance(site):
    r = v1("POST", "/pan/check", site.key, {"pan": "ABCPE1234F"})
    assert r.status_code == 200 and r.json()["holder_type"] == "individual"
    assert r.json()["id"] == f"call_{_last_call().id}"
    assert balance(site) == START - 50

    empty_wallet()
    assert v1("POST", "/pan/check", site.key, {"pan": "ABCPE1234F"}).json()["error"] == {
        "code": "insufficient_balance", "message": "Insufficient balance. Recharge to continue."}
    recharge(100)
    assert v1("POST", "/pan/check", site.key, {"pan": "ABCPE1234F"}).status_code == 200
    assert balance(site) == 50


def test_invalid_number_is_a_charged_answer(site):
    r = v1("POST", "/gstin/check", site.key, {"gstin": "27AAPFU0939F1ZX"})
    assert r.status_code == 200 and r.json()["reason"] == "check_digit"
    assert balance(site) == START - 100


@pytest.mark.parametrize("path,body", [("/pan/check", {}), ("/pan/check", {"pan": 12}), ("/pan/check", ["x"]), ("/gstin/check", {"gstin": "x" * 31})])
def test_bad_input_is_free(site, path, body):
    r = v1("POST", path, site.key, body)
    assert r.status_code == 422 and r.json()["error"]["code"] == "invalid_request"
    assert balance(site) == START


def test_body_that_is_not_json_is_free(site):
    with TestClient(app) as c:
        r = c.post("/v1/pan/check", headers={"Authorization": f"Bearer {site.key}", "Content-Type": "application/json"}, content="{nope")
    assert r.status_code == 422 and r.json()["error"] == {"code": "invalid_request", "message": "Send a JSON body."}


def test_missing_and_bad_keys(site):
    with TestClient(app) as c:
        assert c.post("/v1/pan/check", json={"pan": "ABCPE1234F"}).json()["error"]["code"] == "invalid_key"
    assert v1("POST", "/pan/check", "mcc_nope", {"pan": "ABCPE1234F"}).status_code == 401


# --- IFSC
def test_ifsc_found(site):
    r = v1("GET", "/ifsc/sbin0000691", site.key)
    assert r.status_code == 200
    assert r.json() | {"id": None} == {
        "id": None, "ifsc": "SBIN0000691", "bank": "State Bank of India", "branch": "NEW DELHI MAIN BRANCH",
        "address": "11 SANSAD MARG, NEW DELHI 110 001", "city": "NEW DELHI", "district": "NEW DELHI", "state": "DELHI",
        "micr": "110002087", "upi": True, "neft": True, "rtgs": True, "imps": True, "data_as_of": "2026-09-01",
    }
    assert v1("GET", "/ifsc/AANB0000001", site.key).json()["bank"] is None  # blank in the dataset


def test_ifsc_not_found_is_refunded_and_malformed_is_free(site):
    r = v1("GET", "/ifsc/SBIN0999999", site.key)
    assert r.status_code == 404 and r.json() == {"error": {"code": "not_found", "message": "IFSC not found."}, "id": f"call_{_last_call().id}"}
    assert _last_call().ok is False
    assert v1("GET", "/ifsc/SBIN1234567", site.key).status_code == 422  # 5th character must be 0
    assert balance(site) == START


def test_ifsc_reload_replaces_rows(site):
    with SessionLocal() as db:
        assert ifsc.load(db, io.StringIO(IFSC_CSV.splitlines()[0] + "\n" + IFSC_CSV.splitlines()[1]), "v2", date(2026, 10, 1)) == 1
    assert v1("GET", "/ifsc/AANB0000001", site.key).status_code == 404
    assert v1("GET", "/ifsc/SBIN0000691", site.key).json()["data_as_of"] == "2026-10-01"


# --- Dashboard Try
def test_try_charges_the_balance_like_v1(site):
    r = site.post("/account/try/gstin", json={"gstin": "27AAPFU0939F1ZV"})
    assert r.status_code == 200 and r.json()["format_valid"] is True
    assert _last_call().key_id is None and balance(site) == START - 100

    empty_wallet()
    assert site.post("/account/try/pan", json={"pan": "ABCPE1234F"}).json()["error"]["code"] == "insufficient_balance"
    recharge(50)
    assert site.post("/account/try/pan", json={"pan": "ABCPE1234F"}).status_code == 200
    assert balance(site) == 0
    assert site.get("/account").json()["entries"][0]["note"] == "PAN check"


def test_try_rules(site):
    assert site.post("/account/try/nope", json={}).status_code == 404
    assert site.get("/account").json()["prices"] == {"pan": 50, "gstin": 100, "ifsc": 50, "maps": 500, "maps_locate": 500, "bsa": 2500, "payslip": 1000}
    with TestClient(app, headers={"Origin": "http://evil.test"}) as c:
        c.cookies = site.cookies
        assert c.post("/account/try/pan", json={"pan": "ABCPE1234F"}).status_code == 403
    with TestClient(app, headers=ORIGIN) as c:
        assert c.post("/account/try/pan", json={"pan": "ABCPE1234F"}).status_code == 401


def _last_call():
    with SessionLocal() as db:
        return db.scalar(select(ApiCall).order_by(ApiCall.id.desc()))
