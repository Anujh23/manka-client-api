"""Wallet, the API key and pay-per-call: calls are paid from the prepaid balance, or get a 402."""
import pytest
from fastapi.testclient import TestClient
from pydantic import BaseModel
from sqlalchemy import delete, select

from app import api, products, wallet
from app.db import SessionLocal
from app.main import app
from app.models import Admin, ApiCall, ApiKey, Client, LoginSession, Wallet, WalletEntry
from app.security import hash_password

ORIGIN = {"Origin": "http://site.test"}
PW = "correct horse 1"


# A priced product and a failing one, only for these tests.
class _Empty(BaseModel):
    pass


def _boom(db, body, client):
    raise RuntimeError("boom")


for _path, _price, _run in (("priced", 250, lambda db, body, client: {"ok": True}), ("broken", 250, _boom)):
    _p = products.Product(name=f"_{_path}", title=_path, method="GET", path=f"/v1/_test/{_path}", price_paise=_price, input=_Empty, run=_run)
    app.add_api_route(_p.path, api.endpoint(_p), methods=["GET"])


@pytest.fixture
def site():
    with TestClient(app, headers=ORIGIN) as c:
        with SessionLocal() as db:
            for m in (ApiCall, ApiKey, WalletEntry, Wallet, LoginSession, Admin, Client):
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


def _balance(site):
    return site.get("/account").json()["balance_paise"]


def test_account_shows_balance_and_ledger(site):
    acc = site.get("/account").json()
    assert (acc["balance_paise"], acc["key"]) == (0, None)
    _recharge(100_000)
    acc = site.get("/account").json()
    assert acc["balance_paise"] == 100_000
    assert acc["entries"][0] | {"created_at": None} == {
        "amount_paise": 100_000, "balance_after_paise": 100_000, "kind": "recharge", "note": "UPI UTR 1", "created_at": None,
    }


def test_key_shown_once_then_only_prefix(site):
    new = site.post("/account/keys").json()
    assert new["key"].startswith("mcc_") and new["prefix"] == new["key"][:12]
    shown = site.get("/account").json()["key"]
    assert shown == {k: new[k] for k in ("id", "prefix", "created_at", "last_used_at")}


def test_ping_is_free(site):
    key = site.post("/account/keys").json()["key"]
    r = _call("/v1/ping", key)
    assert r.status_code == 200 and r.json() | {"id": None} == {"id": None, "ok": True, "client": "Asha"}
    assert _call("/v1/ping", "mcc_nope").status_code == 401
    with TestClient(app) as c:
        assert c.get("/v1/ping").status_code == 401


def test_calls_charge_the_balance_and_stop_at_zero(site):  # /v1/_test/priced costs ₹2.50
    key = site.post("/account/keys").json()["key"]
    r = _call("/v1/_test/priced", key)  # new account: ₹0, nothing free
    assert r.status_code == 402 and r.json()["error"] == {"code": "insufficient_balance", "message": "Insufficient balance. Recharge to continue."}
    _recharge(500)
    assert [_call("/v1/_test/priced", key).status_code for _ in range(3)] == [200, 200, 402]
    assert _balance(site) == 0
    assert [e["kind"] for e in site.get("/account").json()["entries"]] == ["usage", "usage", "recharge"]


def test_failed_call_is_refunded(site):
    key = site.post("/account/keys").json()["key"]
    _recharge(1_000)
    r = _call("/v1/_test/broken", key)
    assert r.status_code == 500 and r.json()["error"]["code"] == "server_error"
    acc = site.get("/account").json()
    assert acc["balance_paise"] == 1_000
    assert [e["kind"] for e in acc["entries"][:2]] == ["refund", "usage"]


def test_one_key_regenerate_replaces_it(site):
    old = site.post("/account/keys").json()
    new = site.post("/account/keys").json()
    assert _call("/v1/ping", old["key"]).status_code == 401
    assert _call("/v1/ping", new["key"]).status_code == 200
    assert site.get("/account").json()["key"]["id"] == new["id"]


def test_dashboard_needs_login():
    with TestClient(app, headers=ORIGIN) as c:
        assert c.get("/account").status_code == 401
        assert c.post("/account/keys").status_code == 401
