"""Maps API: create a link (charged), the person shares their GPS fix once, the client reads it back."""
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, select

from app import wallet
from app.db import SessionLocal, utcnow
from app.main import app
from app.models import Admin, ApiCall, ApiKey, Client, LocationRequest, LoginSession, Wallet, WalletEntry
from app.security import hash_password

ORIGIN = {"Origin": "http://site.test"}
PW = "correct horse 1"


@pytest.fixture
def site():
    """Logged-in client 'Asha Finance' with an API key and ₹10."""
    with TestClient(app, headers=ORIGIN) as c:
        with SessionLocal() as db:
            for m in (LocationRequest, ApiCall, ApiKey, WalletEntry, Wallet, LoginSession, Admin, Client):
                db.execute(delete(m))
            db.add_all([
                Client(email="asha@example.com", name="Asha Finance", password_hash=hash_password(PW)),
                Client(email="ravi@example.com", name="Ravi Loans", password_hash=hash_password(PW)),
            ])
            db.commit()
            wallet.credit(db, db.scalar(select(Client.id).where(Client.email == "asha@example.com")), 1_000, "recharge", "UPI UTR 1")
            db.commit()
        assert c.post("/auth/login", json={"email": "asha@example.com", "password": PW}).status_code == 200
        c.key = c.post("/account/keys").json()["key"]
        yield c


def v1(method, path, key, body=None):
    with TestClient(app) as c:  # server-to-server: no cookie, no Origin
        return c.request(method, f"/v1{path}", headers={"Authorization": f"Bearer {key}"}, json=body)


def visitor():
    return TestClient(app, headers=ORIGIN)  # the person opening the link on the website: no cookie


def token_of(link: dict) -> str:
    return link["url"].split("?t=", 1)[1]


def test_link_flow_end_to_end(site):
    r = v1("POST", "/maps/link", site.key, {"reference": " LN-1042 "})
    assert r.status_code == 200
    link = r.json()
    assert link["request"].startswith("loc_") and link["reference"] == "LN-1042" and link["status"] == "pending"
    assert link["url"].startswith("http://site.test/locate/?t=") and link["lat"] is None
    assert site.get("/account").json()["balance_paise"] == 500  # ₹5 per link

    with visitor() as v:
        assert v.get(f"/locate/{token_of(link)}").json() == {"status": "pending", "requester": "Asha Finance"}
        assert v.post(f"/locate/{token_of(link)}", json={"lat": 28.6139391, "lng": 77.2090212, "accuracy_m": 12.34}).status_code == 204
        assert v.post(f"/locate/{token_of(link)}", json={"lat": 1, "lng": 1, "accuracy_m": 5}).status_code == 409  # once only
        assert v.get(f"/locate/{token_of(link)}").json()["status"] == "done"

    done = v1("GET", f"/maps/link/{link['request']}", site.key).json()
    assert (done["status"], done["lat"], done["lng"], done["accuracy_m"], done["url"]) == ("done", 28.6139391, 77.2090212, 12.3, None)
    assert done["located_at"].endswith("Z")
    assert site.get("/account").json()["balance_paise"] == 500  # reading the result is free
    assert [x["request"] for x in site.get("/account/locations").json()] == [link["request"]]


def test_bad_fix_unknown_and_expired_links(site):
    link = v1("POST", "/maps/link", site.key, {"reference": "LN-1"}).json()
    with visitor() as v:
        assert v.post(f"/locate/{token_of(link)}", json={"lat": 91, "lng": 0, "accuracy_m": 5}).status_code == 422
        assert v.post(f"/locate/{token_of(link)}", json={"lat": 1, "lng": 1, "accuracy_m": 0}).status_code == 422
        assert v.get("/locate/nope").status_code == 404
        with SessionLocal() as db:
            db.execute(LocationRequest.__table__.update().values(expires_at=utcnow() - timedelta(minutes=1)))
            db.commit()
        assert v.get(f"/locate/{token_of(link)}").json()["status"] == "expired"
        assert v.post(f"/locate/{token_of(link)}", json={"lat": 1, "lng": 1, "accuracy_m": 5}).status_code == 410
    expired = v1("GET", f"/maps/link/{link['request']}", site.key).json()
    assert (expired["status"], expired["url"]) == ("expired", None)


def test_reference_required_and_results_are_private(site):
    assert v1("POST", "/maps/link", site.key, {}).status_code == 422  # free: never charged
    assert site.get("/account").json()["balance_paise"] == 1_000
    link = v1("POST", "/maps/link", site.key, {"reference": "LN-2"}).json()
    with TestClient(app, headers=ORIGIN) as ravi:
        assert ravi.post("/auth/login", json={"email": "ravi@example.com", "password": PW}).status_code == 200
        ravi_key = ravi.post("/account/keys").json()["key"]
        assert ravi.get("/account/locations").json() == []
    assert v1("GET", f"/maps/link/{link['request']}", ravi_key).status_code == 404  # another client's request
    assert v1("GET", "/maps/link/loc_nope", site.key).status_code == 404


def test_try_from_dashboard(site):
    r = site.post("/account/try/maps", json={"reference": "LN-3"})
    assert r.status_code == 200 and r.json()["status"] == "pending"
    assert site.get("/account").json()["balance_paise"] == 500
