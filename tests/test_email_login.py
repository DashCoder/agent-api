"""Sign in with an email link or code (Amazon SES). The link signs in only the browser that opens it; the code
signs in the browser that asked. Same rules as the OxComply sites."""
import re

import pytest
from fastapi.testclient import TestClient

from app import mailer
from app.config import Settings
from app.main import create_app

SENT = []
LOCAL = {"host": "127.0.0.1:8080"}


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


def code_of(text):
    return re.search(r"Your code: (\d{6})", text).group(1)


def path_of(link):
    return "/" + link.split("://", 1)[1].split("/", 1)[1]


def test_link_signs_in_only_the_browser_that_opens_it(app):
    laptop, phone = TestClient(app), TestClient(app)
    r = laptop.post("/api/auth/email-link", json={"email": "Axel@InAgentic.ai", "next": "/admin/approvals"},
                    headers=LOCAL)
    assert r.status_code == 200 and r.json() == {"sent": True, "email": "axel@inagentic.ai"}
    assert SENT[0]["to"] == "axel@inagentic.ai" and "15 minutes" in SENT[0]["text"]
    link = link_of(SENT[0]["text"])
    assert link.startswith("http://127.0.0.1:8080/api/auth/email-link/verify?t=")
    page = phone.get(path_of(link))
    assert page.status_code == 200 and "signed in as" in page.text and "/admin/approvals" in page.text
    assert phone.get("/api/auth/me").json()["email"] == "axel@inagentic.ai"
    # the asking browser is NOT signed in by someone else opening the link (no planted sessions)
    assert laptop.get("/api/auth/email-link/status").json()["email"] is None
    assert phone.get(path_of(link)).status_code == 400                               # once only


def test_code_signs_in_the_asking_browser(app):
    laptop, other = TestClient(app), TestClient(app)
    laptop.post("/api/auth/email-link", json={"email": "axel@inagentic.ai"}, headers=LOCAL)
    code = code_of(SENT[-1]["text"])
    assert other.post("/api/auth/email-code", json={"code": code}).status_code == 400   # only the asking browser
    assert laptop.post("/api/auth/email-code", json={"code": code}).json()["email"] == "axel@inagentic.ai"
    assert laptop.get("/api/auth/me").json()["email"] == "axel@inagentic.ai"
    assert laptop.post("/api/auth/email-code", json={"code": code}).status_code == 400  # once only
    laptop.post("/api/auth/email-signout")
    assert laptop.get("/api/auth/me").json()["email"] is None


def test_five_wrong_codes_void_the_request(app):
    c = TestClient(app)
    c.post("/api/auth/email-link", json={"email": "axel@inagentic.ai"}, headers=LOCAL)
    code = code_of(SENT[-1]["text"])
    wrong = "000000" if code != "000000" else "111111"
    for _ in range(5):
        assert c.post("/api/auth/email-code", json={"code": wrong}).status_code == 400
    assert c.post("/api/auth/email-code", json={"code": code}).status_code == 400


def test_link_never_points_at_another_host(app):
    c = TestClient(app)
    r = c.post("/api/auth/email-link", json={"email": "a@b.co"}, headers={"host": "evil.example"})
    assert r.status_code == 400 and not SENT
    r = c.post("/api/auth/email-link", json={"email": "a@b.co"},
               headers={"host": "127.0.0.1:8080", "x-forwarded-host": "evil.example"})
    assert r.status_code == 200 and "evil" not in SENT[-1]["text"]


def test_public_url_wins(tmp_path, monkeypatch):
    async def fake_send(s, to, subject, text, html):
        SENT.append({"text": text})
    SENT.clear()
    monkeypatch.setattr(mailer, "send", fake_send)
    s = Settings(data_dir=tmp_path, admin_password="x", secret_key="t", ses_access_key_id="AKIA1",
                 ses_secret_access_key="x", public_url="https://agents.example.com/", _env_file=None)
    c = TestClient(create_app(s, plugins=[]))
    assert c.post("/api/auth/email-link", json={"email": "a@b.co"}, headers={"host": "evil.example"}).status_code == 200
    assert link_of(SENT[-1]["text"]).startswith("https://agents.example.com/api/auth/email-link/verify?t=")


def test_next_stays_on_this_site_and_limits(app):
    c = TestClient(app)
    assert c.post("/api/auth/email-link", json={"email": "nope"}, headers=LOCAL).status_code == 422
    r = c.post("/api/auth/email-link", json={"email": "a@b.co", "next": "https://evil.example/x"}, headers=LOCAL)
    assert r.status_code == 200
    page = c.get(path_of(link_of(SENT[-1]["text"])))
    assert 'href="/admin"' in page.text and "evil" not in page.text
    c.post("/api/auth/email-link", json={"email": "a+1@b.co"}, headers=LOCAL)
    c.post("/api/auth/email-link", json={"email": "A+2@B.co"}, headers=LOCAL)
    # plus-addresses share the real inbox's 3 an hour
    assert c.post("/api/auth/email-link", json={"email": "a+3@b.co"}, headers=LOCAL).status_code == 429
    assert len(SENT) == 3
    assert c.get("/api/auth/email-link/verify?t=bogus").status_code == 400


def test_per_ip_limit(app):
    c = TestClient(app)
    for i in range(10):
        assert c.post("/api/auth/email-link", json={"email": f"p{i}@b.co"}, headers=LOCAL).status_code == 200
    assert c.post("/api/auth/email-link", json={"email": "p99@b.co"}, headers=LOCAL).status_code == 429


def test_without_ses_it_says_so(tmp_path):
    s = Settings(data_dir=tmp_path, admin_password="secret", mock_delay_s=0, secret_key="t", _env_file=None)
    r = TestClient(create_app(s, plugins=[])).post("/api/auth/email-link", json={"email": "a@b.co"})
    assert r.status_code == 503 and "Settings & keys" in r.json()["detail"]


def test_ses_settings_are_on_settings_and_keys(app):
    c = TestClient(app)
    c.post("/api/auth/login", json={"username": "admin", "password": "secret"})
    names = [i["name"] for g in c.get("/api/admin/options").json()["groups"] for i in g["items"]]
    assert {"SES_ACCESS_KEY_ID", "SES_SECRET_ACCESS_KEY", "SES_REGION", "MAIL_FROM"} <= set(names)
