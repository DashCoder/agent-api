"""Sign in with an email link (Amazon SES): any device can open the link; the asking browser signs in by itself."""
import re

import pytest
from fastapi.testclient import TestClient

from app import mailer
from app.config import Settings
from app.main import create_app

SENT = []


@pytest.fixture
def app(tmp_path, monkeypatch):
    async def fake_send(s, to, subject, text, html):
        if not mailer.configured(s):
            raise RuntimeError("Email isn't set up yet")
        SENT.append({"to": to, "subject": subject, "text": text, "html": html})
    SENT.clear()
    monkeypatch.setattr(mailer, "send", fake_send)
    s = Settings(data_dir=tmp_path, admin_password="secret", mock_delay_s=0, secret_key="t", options_restart=False,
                 ses_access_key_id="AKIA1", ses_secret_access_key="x", _env_file=None)
    return create_app(s, plugins=[])


def link_of(text):
    return re.search(r"https?://\S+", text).group(0)


def test_link_signs_in_the_asking_browser_even_when_opened_elsewhere(app):
    laptop, phone = TestClient(app), TestClient(app)
    laptop.get("/api/config")
    r = laptop.post("/api/auth/email-link", json={"email": "Axel@InAgentic.ai", "next": "/admin/approvals"},
                    headers={"host": "127.0.0.1:8080"})
    assert r.status_code == 200 and r.json() == {"sent": True, "email": "axel@inagentic.ai"}
    assert SENT[0]["to"] == "axel@inagentic.ai" and "15 minutes" in SENT[0]["text"]
    link = link_of(SENT[0]["text"])
    assert link.startswith("http://127.0.0.1:8080/api/auth/email-link/verify?t=")    # the address they used
    assert laptop.get("/api/auth/email-link/status").json()["pending"] is True
    page = phone.get(link.split("8080")[1])                                         # opened on the phone
    assert page.status_code == 200 and "Signed in as" in page.text and "/admin/approvals" in page.text
    assert phone.get("/api/auth/me").json()["email"] == "axel@inagentic.ai"
    assert laptop.get("/api/auth/email-link/status").json() == {"email": "axel@inagentic.ai"}
    assert laptop.get("/api/auth/me").json()["email"] == "axel@inagentic.ai"
    # once only
    assert phone.get(link.split("8080")[1]).status_code == 400
    # sign out
    laptop.post("/api/auth/email-signout")
    assert laptop.get("/api/auth/me").json()["email"] is None


def test_safety(app):
    c = TestClient(app)
    assert c.post("/api/auth/email-link", json={"email": "nope"}).status_code == 422
    r = c.post("/api/auth/email-link", json={"email": "a@b.co", "next": "https://evil.example/x"})
    assert r.status_code == 200
    page = c.get(link_of(SENT[-1]["text"]).split("://", 1)[1].split("/", 1)[1].join(["/", ""]))
    assert 'href="/admin"' in page.text and "evil" not in page.text                  # never another site
    for _ in range(4):
        c.post("/api/auth/email-link", json={"email": "a@b.co"})
    assert c.post("/api/auth/email-link", json={"email": "a@b.co"}).status_code == 429
    assert c.get("/api/auth/email-link/verify?t=bogus").status_code == 400


def test_without_ses_it_says_so(tmp_path):
    s = Settings(data_dir=tmp_path, admin_password="secret", mock_delay_s=0, secret_key="t", _env_file=None)
    r = TestClient(create_app(s, plugins=[])).post("/api/auth/email-link", json={"email": "a@b.co"})
    assert r.status_code == 503 and "Settings & keys" in r.json()["detail"]


def test_ses_settings_are_on_settings_and_keys(app):
    c = TestClient(app)
    c.post("/api/auth/login", json={"username": "admin", "password": "secret"})
    names = [i["name"] for g in c.get("/api/admin/options").json()["groups"] for i in g["items"]]
    assert {"SES_ACCESS_KEY_ID", "SES_SECRET_ACCESS_KEY", "SES_REGION", "MAIL_FROM"} <= set(names)
