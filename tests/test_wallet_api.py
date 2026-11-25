"""Wallet, API keys and pay-per-call: balance → keys → charged /v1 calls."""
import pytest
from fastapi import Depends, HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import delete, select

from app import api, products, wallet
from app.db import SessionLocal
from app.main import app
from app.models import ApiCall, ApiKey, Client, LoginSession, TestWallet, Wallet, WalletEntry
from app.security import hash_password

ORIGIN = {"Origin": "http://site.test"}
PW = "correct horse 1"


# A priced product and a failing one, only for these tests.
@app.get("/v1/_test/priced")
def _priced(caller=Depends(api.charged("ping"))):
    return {"ok": True}


@app.get("/v1/_test/broken")
def _broken(caller=Depends(api.charged("ping"))):
    raise HTTPException(400, "Bad input.")


@pytest.fixture
def site():
    with TestClient(app, headers=ORIGIN) as c:
        with SessionLocal() as db:
            for m in (ApiCall, ApiKey, WalletEntry, Wallet, TestWallet, LoginSession, Client):
                db.execute(delete(m))
            db.add(Client(email="asha@example.com", name="Asha", password_hash=hash_password(PW)))
            db.commit()
        assert c.post("/auth/login", json={"email": "asha@example.com", "password": PW}).status_code == 200
        yield c


def _client_id():
    with SessionLocal() as db:
        return db.scalar(select(Client.id).where(Client.email == "asha@example.com"))


def _recharge(paise):
    with SessionLocal() as db:
        wallet.credit(db, _client_id(), paise, "recharge", "UPI UTR 1")
        db.commit()


def _call(path, key):
    with TestClient(app) as c:  # server-to-server: no cookie, no Origin
        return c.get(path, headers={"Authorization": f"Bearer {key}"})


def test_account_shows_balance_and_ledger(site):
    acc = site.get("/account").json()
    assert (acc["balance_paise"], acc["test_balance_paise"]) == (0, products.TEST_CREDIT_PAISE)
    _recharge(100_000)
    acc = site.get("/account").json()
    assert acc["balance_paise"] == 100_000
    assert acc["entries"][0] | {"created_at": None} == {
        "amount_paise": 100_000, "balance_after_paise": 100_000, "kind": "recharge", "note": "UPI UTR 1", "created_at": None,
    }


def test_key_shown_once_then_only_prefix(site):
    new = site.post("/account/keys", json={"mode": "live"}).json()
    assert new["key"].startswith("mcc_live_") and new["prefix"] == new["key"][:13]
    listed = site.get("/account").json()["keys"]
    assert listed == [{k: new[k] for k in ("id", "prefix", "mode", "created_at", "last_used_at")}]
    assert "key" not in listed[0]


def test_ping_with_test_and_live_keys(site):
    test_key = site.post("/account/keys", json={"mode": "test"}).json()["key"]
    r = _call("/v1/ping", test_key)
    assert r.status_code == 200 and r.json() == {"ok": True, "mode": "test", "client": "Asha"}
    assert _call("/v1/ping", "mcc_live_nope").status_code == 401
    with TestClient(app) as c:
        assert c.get("/v1/ping").status_code == 401


def test_live_calls_charge_and_stop_at_zero(site, monkeypatch):
    monkeypatch.setitem(products.PRICES, "ping", 250)  # ₹2.50 per call
    live = site.post("/account/keys", json={"mode": "live"}).json()["key"]
    test = site.post("/account/keys", json={"mode": "test"}).json()["key"]

    assert _call("/v1/_test/priced", live).status_code == 402  # empty wallet
    _recharge(500)
    assert _call("/v1/_test/priced", live).status_code == 200
    assert _call("/v1/_test/priced", live).status_code == 200
    assert _call("/v1/_test/priced", live).status_code == 402  # ₹0 left
    assert _call("/v1/_test/priced", test).status_code == 200  # test keys use the test credit, not the wallet
    acc = site.get("/account").json()
    assert (acc["balance_paise"], acc["test_balance_paise"]) == (0, products.TEST_CREDIT_PAISE - 250)


def test_test_credit_is_capped_at_100_rupees(site, monkeypatch):
    monkeypatch.setitem(products.PRICES, "ping", 25_00)  # ₹25 per call → 4 test calls fit in ₹100
    test = site.post("/account/keys", json={"mode": "test"}).json()["key"]
    assert [_call("/v1/_test/priced", test).status_code for _ in range(4)] == [200] * 4
    r = _call("/v1/_test/priced", test)
    assert r.status_code == 402 and "Test credit used up" in r.json()["detail"]
    acc = site.get("/account").json()
    assert (acc["balance_paise"], acc["test_balance_paise"], acc["entries"]) == (0, 0, [])  # real wallet untouched


def test_failed_call_is_refunded(site, monkeypatch):
    monkeypatch.setitem(products.PRICES, "ping", 250)
    live = site.post("/account/keys", json={"mode": "live"}).json()["key"]
    _recharge(1_000)
    assert _call("/v1/_test/broken", live).status_code == 400
    acc = site.get("/account").json()
    assert acc["balance_paise"] == 1_000
    assert [e["kind"] for e in acc["entries"][:2]] == ["refund", "usage"]

    test = site.post("/account/keys", json={"mode": "test"}).json()["key"]
    assert _call("/v1/_test/broken", test).status_code == 400
    assert site.get("/account").json()["test_balance_paise"] == products.TEST_CREDIT_PAISE  # test credit refunded too


def test_revoked_key_stops_working(site):
    new = site.post("/account/keys", json={"mode": "test"}).json()
    assert site.post(f"/account/keys/{new['id']}/revoke").status_code == 204
    assert _call("/v1/ping", new["key"]).status_code == 401
    assert site.get("/account").json()["keys"] == []


def test_dashboard_needs_login():
    with TestClient(app, headers=ORIGIN) as c:
        assert c.get("/account").status_code == 401
        assert c.post("/account/keys", json={"mode": "test"}).status_code == 401
