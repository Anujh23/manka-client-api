"""Render/Neon specifics: host lock, Postgres URL normalisation, Brevo HTTPS mail."""
import json

from fastapi.testclient import TestClient

from app import mailer
from app.config import Settings, get_settings
from app.main import app, settings


def test_neon_url_gets_psycopg_driver():
    for raw in ("postgres://u:p@ep-x.neon.tech/db?sslmode=require", "postgresql://u:p@ep-x.neon.tech/db?sslmode=require"):
        assert Settings(database_url=raw).database_url == "postgresql+psycopg://u:p@ep-x.neon.tech/db?sslmode=require"
    assert Settings(database_url="sqlite:///./x.db").database_url == "sqlite:///./x.db"


def test_only_custom_domain_answers(monkeypatch):
    monkeypatch.setattr(settings, "allowed_hosts", "api.example.in")
    with TestClient(app, base_url="https://mankaconsulting.onrender.com") as c:
        assert c.get("/auth/me").status_code == 404
        assert c.get("/health").status_code == 200  # platform health checks still pass
    with TestClient(app, base_url="https://api.example.in") as c:
        assert c.get("/auth/me").status_code == 401  # reaches the app


def test_reset_mail_goes_through_brevo_api(monkeypatch):
    sent = {}

    class Res:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def read(self): return b"{}"

    def fake_urlopen(req, timeout):
        sent.update(url=req.full_url, key=req.get_header("Api-key"), body=json.loads(req.data))
        return Res()

    s = get_settings()
    monkeypatch.setattr(s, "brevo_api_key", "test-key")
    monkeypatch.setattr(s, "mail_from", "contact@example.in")
    monkeypatch.setattr(mailer.urllib.request, "urlopen", fake_urlopen)
    mailer.send_reset_link("asha@example.com", "Asha", "https://site/reset/?token=t")

    assert sent["url"] == mailer.BREVO_URL and sent["key"] == "test-key"
    assert sent["body"]["to"] == [{"email": "asha@example.com", "name": "Asha"}]
    assert "https://site/reset/?token=t" in sent["body"]["textContent"]
