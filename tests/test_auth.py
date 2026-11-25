import re

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete

from app import mailer
from app.db import SessionLocal
from app.main import app
from app.models import Client, LoginAttempt, LoginSession, PasswordReset
from app.security import hash_password

ORIGIN = {"Origin": "http://site.test"}
PW = "correct horse 1"


@pytest.fixture
def api():
    with TestClient(app, headers=ORIGIN) as c:
        with SessionLocal() as db:
            for m in (LoginAttempt, LoginSession, PasswordReset, Client):
                db.execute(delete(m))
            db.add(Client(email="asha@example.com", name="Asha", password_hash=hash_password(PW)))
            db.commit()
        yield c


def login(api, email="asha@example.com", password=PW):
    return api.post("/auth/login", json={"email": email, "password": password})


def test_login_me_logout(api):
    r = login(api, email="  Asha@Example.com ")
    assert r.status_code == 200 and r.json() == {"name": "Asha", "email": "asha@example.com"}
    assert "httponly" in r.headers["set-cookie"].lower()
    assert api.get("/auth/me").json()["name"] == "Asha"
    assert api.post("/auth/logout").status_code == 204
    assert api.get("/auth/me").status_code == 401


def test_wrong_password_and_unknown_email_look_the_same(api):
    a, b = login(api, password="nope nope nope"), login(api, email="ghost@example.com")
    assert a.status_code == b.status_code == 401
    assert a.json() == b.json()


def test_lockout_after_five_failures(api):
    for _ in range(5):
        assert login(api, password="wrong wrong").status_code == 401
    assert login(api).status_code == 429


def test_disabled_client_cannot_log_in(api):
    with SessionLocal() as db:
        db.query(Client).update({"is_active": False})
        db.commit()
    assert login(api).status_code == 401


def test_foreign_origin_is_rejected(api):
    r = api.post("/auth/login", json={"email": "asha@example.com", "password": PW}, headers={"Origin": "http://evil.test"})
    assert r.status_code == 403


def test_forgot_and_reset(api, monkeypatch):
    sent = []
    monkeypatch.setattr(mailer, "send_reset_link", lambda to, name, link: sent.append(link))

    assert api.post("/auth/forgot", json={"email": "ghost@example.com"}).status_code == 204
    assert sent == []

    login(api)  # an existing session must die on reset
    assert api.post("/auth/forgot", json={"email": "asha@example.com"}).status_code == 204
    assert len(sent) == 1 and sent[0].startswith("http://site.test/clientlogin/reset/?token=")
    token = re.search(r"token=(.+)$", sent[0]).group(1)

    assert api.post("/auth/reset", json={"token": token, "password": "short"}).status_code == 422
    assert api.post("/auth/reset", json={"token": token, "password": "brand new pass"}).status_code == 204
    assert api.get("/auth/me").status_code == 401
    assert api.post("/auth/reset", json={"token": token, "password": "another one!!"}).status_code == 400
    assert login(api).status_code == 401
    assert login(api, password="brand new pass").status_code == 200
