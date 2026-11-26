"""Maps JavaScript API: the script, browser calls from listed websites only, CORS for any site, billing, map images."""
import io
from urllib.parse import urlparse

import pytest
from fastapi.testclient import TestClient
from PIL import Image
from sqlalchemy import delete, select

from app import staticmap, wallet
from app.db import SessionLocal
from app.main import app
from app.models import Admin, ApiCall, ApiKey, Client, LocationRequest, LoginSession, Wallet, WalletEntry, Website
from app.security import hash_password

ORIGIN = {"Origin": "http://site.test"}
PW = "correct horse 1"
SHOP = "https://shop.example.in"


@pytest.fixture
def site():
    """Logged-in client with an API key and ₹10."""
    with TestClient(app, headers=ORIGIN) as c:
        with SessionLocal() as db:
            for m in (Website, LocationRequest, ApiCall, ApiKey, WalletEntry, Wallet, LoginSession, Admin, Client):
                db.execute(delete(m))
            db.add(Client(email="asha@example.com", name="Asha Finance", password_hash=hash_password(PW)))
            db.commit()
            wallet.credit(db, db.scalar(select(Client.id)), 1_000, "recharge", "UPI UTR 1")
            db.commit()
        assert c.post("/auth/login", json={"email": "asha@example.com", "password": PW}).status_code == 200
        c.key = c.post("/account/keys").json()["key"]
        yield c


def browser(origin=SHOP):
    return TestClient(app, headers={"Origin": origin})  # a client's web page calling /v1


def balance(site):
    return site.get("/account").json()["balance_paise"]


def test_script_is_served():
    with TestClient(app) as c:
        r = c.get("/v1/maps.js?key=anything")
    assert r.status_code == 200 and r.headers["content-type"].startswith("application/javascript")
    assert "window.MapsAPI" in r.text and "/maps/locate" in r.text


FIX = {"lat": 28.6315, "lng": 77.2167, "accuracy_m": 12.34}


def test_browser_calls_need_a_listed_website(site):
    auth = {"Authorization": f"Bearer {site.key}"}
    with browser() as b:
        r = b.post("/v1/maps/locate", headers=auth, json=FIX)
        assert r.status_code == 403 and r.json()["error"]["code"] == "origin_not_allowed"
        assert r.headers["access-control-allow-origin"] == "*"  # the page can read the error
    assert balance(site) == 1_000  # refused before charging

    assert site.post("/account/websites", json={"websites": [" https://Shop.Example.in/ ", "http://localhost:5500", ""]}).json() == [
        "http://localhost:5500", SHOP]
    assert site.get("/account").json()["websites"] == ["http://localhost:5500", SHOP]
    with browser() as b:
        loc = b.post("/v1/maps/locate", headers=auth, json=FIX).json()
        assert (loc["lat"], loc["lng"], loc["accuracy_m"]) == (28.6315, 77.2167, 12.3) and loc["request"].startswith("loc_")
    with browser("https://copycat.example.com") as b:
        assert b.post("/v1/maps/locate", headers=auth, json=FIX).status_code == 403
    assert site.post("/v1/maps/load", headers=auth).status_code == 404  # showing a map is free: no API call
    assert balance(site) == 1_000 - 500
    assert [(x["reference"], x["status"]) for x in site.get("/account/locations").json()] == [("Browser", "done")]


def test_server_calls_unaffected_and_preflight_open(site):
    with TestClient(app) as server:  # no Origin: curl / backend
        assert server.post("/v1/maps/locate", headers={"Authorization": f"Bearer {site.key}"}, json=FIX).status_code == 200
    with browser("https://anyone.example") as b:
        r = b.options("/v1/maps/locate", headers={"Access-Control-Request-Method": "POST", "Access-Control-Request-Headers": "authorization,content-type"})
        assert r.status_code == 204 and r.headers["access-control-allow-origin"] == "*"
        assert "Authorization" in r.headers["access-control-allow-headers"]


TILE_COLOUR = (200, 220, 240)  # conftest's fake tile


def _no_tile(z, x, y):
    raise OSError("tile server down")


def test_located_requests_have_a_map_image(site, monkeypatch):
    auth = {"Authorization": f"Bearer {site.key}"}
    with TestClient(app) as server:
        loc = server.post("/v1/maps/locate", headers=auth, json=FIX).json()
        assert (28.6315, 77.2167, 12.3, False) in staticmap._images  # rendering started as soon as it was saved
        path = urlparse(loc["image"]).path
        assert path == f"/v1/maps/image/{loc['request']}.png"
        r = server.get(path)  # no key: it goes straight into an <img>
        assert r.status_code == 200 and r.headers["content-type"] == "image/png" and "immutable" in r.headers["cache-control"]
        img = Image.open(io.BytesIO(r.content))
        assert img.size == (640, 400) and img.getpixel((637, 397)) == TILE_COLOUR  # no credit by default
        labelled = Image.open(io.BytesIO(server.get(path, params={"credit": "1"}).content))
        assert labelled.getpixel((637, 397)) != TILE_COLOUR  # credit=1: the label sits in the corner
        assert balance(site) == 1_000 - 500  # the image is included

        link = server.post("/v1/maps/link", headers=auth, json={"reference": "LN-1"}).json()
        assert link["image"] is None and server.get(f"/v1/maps/image/{link['request']}.png").status_code == 404  # not located yet
        assert server.get("/v1/maps/image/loc_nope.png").status_code == 404

        monkeypatch.setattr(staticmap, "tile", _no_tile)
        staticmap._images.clear()
        r = server.get(path)
        assert r.status_code == 200 and r.headers["cache-control"] == "no-store"  # blank map, pin still there; retried next time


def test_websites_validation_and_csrf_still_on_for_account(site):
    assert site.post("/account/websites", json={"websites": ["not a site"]}).status_code == 400
    assert site.post("/account/websites", json={"websites": ["ftp://x.in"]}).status_code == 400
    assert site.post("/account/websites", json={"websites": [f"https://s{i}.in" for i in range(11)]}).status_code == 422
    with browser("https://evil.example") as b:  # cookie endpoints keep the origin check
        b.cookies = site.cookies
        assert b.post("/account/websites", json={"websites": []}).status_code == 403
