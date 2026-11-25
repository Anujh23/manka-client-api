"""Admin: manage clients from the browser. Only accounts in `admins` may use /admin."""
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, select

from app.db import SessionLocal
from app.main import app
from app.models import Admin, ApiCall, ApiKey, Client, LoginSession, TestWallet, Wallet, WalletEntry
from app.security import hash_password

ORIGIN = {"Origin": "http://site.test"}
PW = "correct horse 1"


def _login(c, email, pw=PW):
    return c.post("/auth/login", json={"email": email, "password": pw})


@pytest.fixture
def admin():
    with TestClient(app, headers=ORIGIN) as c:
        with SessionLocal() as db:
            for m in (ApiCall, ApiKey, WalletEntry, Wallet, TestWallet, LoginSession, Admin, Client):
                db.execute(delete(m))
            boss = Client(email="boss@example.com", name="Boss", password_hash=hash_password(PW))
            db.add_all([boss, Client(email="asha@example.com", name="Asha", password_hash=hash_password(PW))])
            db.flush()
            db.add(Admin(client_id=boss.id))
            db.commit()
        assert _login(c, "boss@example.com").status_code == 200
        yield c


def _id(email):
    with SessionLocal() as db:
        return db.scalar(select(Client.id).where(Client.email == email))


def test_non_admin_is_refused():
    with TestClient(app, headers=ORIGIN) as c:
        assert c.get("/admin/clients").status_code == 401  # not logged in
    with TestClient(app, headers=ORIGIN) as c, SessionLocal() as db:
        db.execute(delete(Admin)); db.commit()
        if not db.scalar(select(Client).where(Client.email == "asha@example.com")):
            db.add(Client(email="asha@example.com", name="Asha", password_hash=hash_password(PW))); db.commit()
        assert _login(c, "asha@example.com").status_code == 200
        assert c.get("/admin/clients").status_code == 403
        assert c.post("/admin/clients", json={"name": "X", "email": "x@example.com", "password": PW}).status_code == 403


def test_list_and_account_flag(admin):
    rows = admin.get("/admin/clients").json()
    assert {(r["email"], r["is_admin"], r["balance_paise"]) for r in rows} == {("boss@example.com", True, 0), ("asha@example.com", False, 0)}
    assert admin.get("/account").json()["is_admin"] is True


def test_create_edit_and_duplicate_email(admin):
    r = admin.post("/admin/clients", json={"name": "Acme Finance", "email": " Ops@Acme.in ", "password": PW})
    assert r.status_code == 200 and r.json()["email"] == "ops@acme.in"
    with TestClient(app, headers=ORIGIN) as c:
        assert _login(c, "ops@acme.in").status_code == 200  # the new client can log in

    assert admin.post("/admin/clients", json={"name": "Dup", "email": "ops@acme.in", "password": PW}).status_code == 400
    assert admin.post("/admin/clients", json={"name": "Bad", "email": "not-an-email", "password": PW}).status_code == 400
    assert admin.post("/admin/clients", json={"name": "Short", "email": "s@acme.in", "password": "short"}).status_code == 422

    cid = r.json()["id"]
    e = admin.post(f"/admin/clients/{cid}", json={"name": "Acme Finance Ltd", "email": "accounts@acme.in"})
    assert (e.json()["name"], e.json()["email"]) == ("Acme Finance Ltd", "accounts@acme.in")
    assert admin.post(f"/admin/clients/{cid}", json={"name": "X", "email": "asha@example.com"}).status_code == 400


def test_password_reset_logs_client_out(admin):
    with TestClient(app, headers=ORIGIN) as asha:
        assert _login(asha, "asha@example.com").status_code == 200
        assert admin.post(f"/admin/clients/{_id('asha@example.com')}/password", json={"password": "brand new pass"}).status_code == 204
        assert asha.get("/auth/me").status_code == 401
        assert _login(asha, "asha@example.com", "brand new pass").status_code == 200


def test_disable_blocks_login_and_api_keys_then_enable(admin):
    aid = _id("asha@example.com")
    with TestClient(app, headers=ORIGIN) as asha:
        _login(asha, "asha@example.com")
        key = asha.post("/account/keys", json={"mode": "test"}).json()["key"]
        assert admin.post(f"/admin/clients/{aid}/disable").status_code == 204
        assert asha.get("/auth/me").status_code == 401
        assert _login(asha, "asha@example.com").status_code == 401
        with TestClient(app) as srv:
            assert srv.get("/v1/ping", headers={"Authorization": f"Bearer {key}"}).status_code == 401
        assert admin.post(f"/admin/clients/{aid}/enable").status_code == 204
        assert _login(asha, "asha@example.com").status_code == 200
    assert admin.post(f"/admin/clients/{_id('boss@example.com')}/disable").status_code == 400  # not yourself


def test_wallet_credit_and_debit_never_below_zero(admin):
    aid = _id("asha@example.com")
    r = admin.post(f"/admin/clients/{aid}/wallet", json={"action": "credit", "rupees": "500", "note": "UPI UTR 1"})
    assert r.json()["balance_paise"] == 50_000
    r = admin.post(f"/admin/clients/{aid}/wallet", json={"action": "debit", "rupees": "120.50", "note": "Correction"})
    assert r.json()["balance_paise"] == 37_950
    assert admin.post(f"/admin/clients/{aid}/wallet", json={"action": "debit", "rupees": "1000", "note": "x"}).status_code == 400
    assert admin.post(f"/admin/clients/{aid}/wallet", json={"action": "credit", "rupees": "0", "note": "x"}).status_code == 422
    with TestClient(app, headers=ORIGIN) as asha:
        _login(asha, "asha@example.com")
        entries = asha.get("/account").json()["entries"]
        assert [(e["kind"], e["amount_paise"]) for e in entries] == [("adjustment", -12_050), ("recharge", 50_000)]
        assert entries[1]["note"] == "UPI UTR 1 (by boss@example.com)"
