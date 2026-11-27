import hashlib
import hmac
import json
import re
import time
from types import SimpleNamespace
from urllib.parse import parse_qs, urlencode, urlsplit
from uuid import uuid4

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient

from app.main import create_app
from app.settings import Settings
from app.social_login import ISSUER, identity_subject
from test_checkout import csrf_headers, key
from test_checkout_options import options
from test_merchants import ORIGIN, OWNER, SETUP, form_token, register

PASSWORD = "fixture long password"


def token(client, action):
    page = client.get("/account")
    match = re.search(r'<form action="' + re.escape(action) + r'" method="post"[^>]*>\s*<input type="hidden" name="token" value="([^"]+)"', page.text)
    return match[1]


def password(client, username, action="register"):
    path = f"/account/password/{action}"
    return client.post(path, data={"token": token(client, path), "username": username, "password": PASSWORD},
                       headers={"Origin": ORIGIN})


def test_guest_claim_and_paid_order_sync_across_devices(tmp_path):
    app = create_app(Settings(str(tmp_path / "accounts.sqlite3"), ORIGIN, setup_key=SETUP,
                              default_owner_code=OWNER, accounts_enabled=True))
    with (
        TestClient(app, base_url=ORIGIN) as phone, TestClient(app, base_url=ORIGIN) as laptop,
        TestClient(app, base_url=ORIGIN) as old_guest,
    ):
        merchant_csrf = register(phone, "coffee")
        assert options(phone, merchant_csrf, "receipt_download").status_code == 200
        headers = csrf_headers(phone)
        order = phone.post("/api/v1/orders", headers=key(headers)).json()
        url = "/api/v1/orders/" + order["id"]
        quote = phone.post(url + "/messages", headers=headers, json={
            "order_version": order["version"], "text": "20 завтраков, из них 5 вегетарианских",
            "client_message_id": str(uuid4()),
        }).json()["order"]
        payment = phone.post(url + "/payment-attempts", headers=key(headers), json={"order_version": quote["version"]})
        location = payment.headers["Location"]
        old_guest.cookies.update(phone.cookies)
        assert password(phone, "tester").status_code == 200
        assert phone.get("/merchant", follow_redirects=False).status_code == 303
        assert phone.get("/account").text.find(order["id"]) == -1
        claim = "/account/claim"
        assert phone.post(claim, data={"token": token(phone, claim)}, headers={"Origin": ORIGIN}).status_code == 200
        assert order["id"] in phone.get("/account").text
        assert old_guest.get(url).status_code == 401

        assert password(laptop, "tester", "login").status_code == 200
        assert location.removeprefix(url).startswith("/payment-attempts/")
        assert laptop.get(f"/s/coffee/orders/{order['id']}").status_code == 200
        assert laptop.get(location).json()["status"] == "pending"
        headers = csrf_headers(laptop)
        assert laptop.post("/demo" + location.removeprefix("/api/v1") + "/approve", headers=headers, json={}).status_code == 200
        assert phone.get(url).json()["status"] == "paid"
        confirm = f"/s/coffee/orders/{order['id']}/documents/{payment.json()['id']}/confirmation"
        assert laptop.get(confirm).status_code == 200

        assert password(old_guest, "other").status_code == 200
        old_guest.get("/")
        assert old_guest.get(url).status_code == 404 and old_guest.get(confirm).status_code == 404
        logout = "/account/logout"
        assert phone.post(logout, data={"token": token(phone, logout), "everywhere": "true"},
                          headers={"Origin": ORIGIN}).status_code == 200
        assert laptop.get(url).status_code == 401


def test_only_shop_owner_code_grants_seller_role_on_other_devices(tmp_path):
    app = create_app(Settings(str(tmp_path / "seller.sqlite3"), ORIGIN, setup_key=SETUP,
                              default_owner_code=OWNER, accounts_enabled=True))
    with TestClient(app, base_url=ORIGIN) as first, TestClient(app, base_url=ORIGIN) as second:
        assert password(first, "seller").status_code == 200
        assert first.get("/merchant", follow_redirects=False).status_code == 303
        register(first, "coffee")
        assert password(second, "seller", "login").status_code == 200
        page = second.get("/merchant", follow_redirects=False)
        assert page.status_code == 200 and "Настройки оплаты" in page.text
        assert options(second, form_token(page.text), "tips").status_code == 200
        assert 'name="tips" value="true" checked' in first.get("/merchant").text
        old_csrf = form_token(first.get("/merchant").text)
        register(first, "cleaning")
        assert options(first, old_csrf, "tips").status_code == 403


def start_social(client, provider):
    path = "/account/oauth/" + provider
    response = client.post(path, data={"token": token(client, path)}, headers={"Origin": ORIGIN}, follow_redirects=False)
    assert response.status_code == 303
    return parse_qs(urlsplit(response.headers["Location"]).query)


@pytest.mark.parametrize("provider", ["google", "yandex", "vk", "telegram"])
def test_oauth_callback_binds_browser_and_is_single_use(tmp_path, monkeypatch, provider):
    settings = Settings(str(tmp_path / "oauth.sqlite3"), ORIGIN, accounts_enabled=True,
                        social_clients={provider: ["client", "fixture-secret"]})
    app = create_app(settings)
    calls = []
    monkeypatch.setattr("app.account_views.identity_subject", lambda *args: calls.append(args) or "subject")
    with TestClient(app, base_url=ORIGIN) as first, TestClient(app, base_url=ORIGIN) as second:
        assert first.get("/account").headers["Referrer-Policy"] == "same-origin"
        assert password(first, "tester").status_code == 200
        params = start_social(first, provider)
        callback = f"/account/oauth/{provider}/callback"
        query = {"code": "fixture", "state": params["state"][0], "device_id": "device"}
        assert second.get(callback, params=query, follow_redirects=False).headers["Location"].endswith("interrupted")
        assert not calls
        response = first.get(callback, params=query, follow_redirects=False)
        assert response.headers["Location"] == "/account" and response.headers["Referrer-Policy"] == "no-referrer"
        assert first.get(callback, params=query, follow_redirects=False).headers["Location"].endswith("interrupted")
        assert len(calls) == 1
        params = start_social(second, provider)
        assert second.get(callback, params={**query, "state": params["state"][0]}).status_code == 200
        assert "Логин <strong>tester</strong>" in second.get("/account").text


def test_oauth_revokes_strict_seller_cookie_and_stale_registration(tmp_path, monkeypatch):
    settings = Settings(str(tmp_path / "older-forms.sqlite3"), ORIGIN, accounts_enabled=True,
                        setup_key=SETUP, default_owner_code=OWNER, social_clients={"google": ["client", "fixture-secret"]})
    app = create_app(settings)
    monkeypatch.setattr("app.account_views.identity_subject", lambda *_: "google-subject")
    with TestClient(app, base_url=ORIGIN) as current, TestClient(app, base_url=ORIGIN) as previous:
        register(current, "coffee")
        previous.cookies.update(current.cookies)
        assert previous.get("/merchant").status_code == 200
        path = "/account/password/register"
        stale_token = token(current, path)
        oauth = start_social(current, "google")
        current.cookies.delete("__Host-merchant_session")
        response = current.get("/account/oauth/google/callback",
                               params={"state": oauth["state"][0], "code": "fixture"}, follow_redirects=False)
        assert response.headers["Location"] == "/account"
        assert previous.get("/merchant", follow_redirects=False).status_code == 303
        assert current.post(path, data={"token": stale_token, "username": "from-old-tab", "password": PASSWORD},
                            headers={"Origin": ORIGIN}).status_code == 403
        assert "Логин <strong>" not in current.get("/account").text
        add = "/account/password/add"
        assert current.post(add, data={"token": token(current, add), "username": "chosen-login", "password": PASSWORD},
                            headers={"Origin": ORIGIN}).status_code == 200
        assert password(previous, "chosen-login", "login").status_code == 200


def test_email_and_max_prove_identity_before_issuing_session(tmp_path, monkeypatch):
    settings = Settings(str(tmp_path / "proof.sqlite3"), ORIGIN, accounts_enabled=True, mode="live",
                        mail_host="mail.example.test", mail_from="orders@example.test",
                        max_bot_name="FixtureBot", max_bot_token="fixture-max-token")
    app = create_app(settings)
    captured = []

    class SMTP:
        def __init__(self, *args, **kwargs): pass
        def __enter__(self): return self
        def __exit__(self, *args): return False
        def send_message(self, message): captured.append(message.get_content())

    monkeypatch.setattr("app.account_views.smtplib.SMTP_SSL", SMTP)
    with TestClient(app, base_url=ORIGIN) as first, TestClient(app, base_url=ORIGIN) as second:
        email = "/account/email"
        response = first.post(email, data={"token": token(first, email), "email": "buyer@example.test"}, headers={"Origin": ORIGIN})
        assert response.status_code == 200
        url = captured[0].splitlines()[-1]
        assert second.get(url, follow_redirects=False).headers["Location"].endswith("interrupted")
        assert first.get(url, follow_redirects=False).headers["Location"] == "/account"
        start = "/account/max"
        page = first.post(start, data={"token": token(first, start)}, headers={"Origin": ORIGIN})
        state = re.search(r'name="state" value="([^"]+)"', page.text)[1]
        form = {"token": form_token(page.text), "state": state}
        finish = start + "/finish"
        assert first.post(finish, data=form, headers={"Origin": ORIGIN}).status_code == 400
        fields = {"auth_date": str(int(time.time())), "start_param": state, "user": json.dumps({"id": 570})}
        signature_key = hmac.digest(b"WebAppData", settings.max_bot_token.encode(), "sha256")
        signature = hmac.new(signature_key, "\n".join(f"{k}={v}" for k, v in sorted(fields.items())).encode(), hashlib.sha256).hexdigest()
        webapp = start + "/miniapp"
        bad = urlencode({**fields, "user": json.dumps({"id": 571}), "hash": signature})
        assert second.post(webapp, json={"init_data": bad}, headers={"Origin": ORIGIN}).status_code == 401
        signed = urlencode({**fields, "hash": signature})
        verified = second.post(webapp, json={"init_data": signed}, headers={"Origin": ORIGIN})
        assert verified.status_code == 200
        code = verified.json()["code"]
        assert second.post(webapp, json={"init_data": signed}, headers={"Origin": ORIGIN}).status_code == 400
        assert first.post(finish, data=form, headers={"Origin": ORIGIN}).status_code == 400
        assert "Почта, MAX" not in first.get("/account").text
        assert second.post(finish, data={**form, "code": code}, headers={"Origin": ORIGIN}).status_code == 403
        assert first.post(finish, data={**form, "code": code}, headers={"Origin": ORIGIN}).status_code == 200
        assert first.post(finish, data={**form, "code": code}, headers={"Origin": ORIGIN}).status_code == 403
        assert "Подключены" in first.get("/account").text and "Почта, MAX" in first.get("/account").text


@pytest.mark.parametrize("provider", ["google", "telegram"])
def test_oidc_subject_requires_signature_audience_and_google_nonce(monkeypatch, provider):
    now = int(time.time())
    private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    token = jwt.encode({"iss": ISSUER[provider], "aud": "client", "sub": "42", "iat": now, "exp": now + 90, "nonce": "expected"},
                       private, algorithm="RS256")

    def exchange(url, **kwargs):
        assert kwargs["data"]["code_verifier"] == "verifier" and kwargs["follow_redirects"] is False
        return httpx.Response(200, json={"id_token": token}, request=httpx.Request("POST", url))

    monkeypatch.setattr("app.social_login.httpx.post", exchange)
    monkeypatch.setattr(jwt.PyJWKClient, "get_signing_key_from_jwt", lambda *_: SimpleNamespace(key=private.public_key()))
    assert identity_subject(provider, ORIGIN, ["client", "fixture-secret"], "code", "verifier", "expected", "state", "") == "42"
    with pytest.raises(jwt.InvalidAudienceError):
        identity_subject(provider, ORIGIN, ["other", "fixture-secret"], "code", "verifier", "expected", "state", "")
    if provider == "google":
        with pytest.raises(ValueError, match="попытке"):
            identity_subject(provider, ORIGIN, ["client", "fixture-secret"], "code", "verifier", "other", "state", "")


def test_private_order_history_follows_corporate_subject(tmp_path, monkeypatch):
    settings = Settings(
        str(tmp_path / "corporate.sqlite3"), ORIGIN, access="private", accounts_enabled=True,
        oidc_issuer="https://login.example.test", oidc_jwks="https://login.example.test/jwks",
        oidc_client_id="fixture-client", oidc_email_domain="example.test",
    )
    monkeypatch.setattr("app.main.CorporateIdentity.verify", lambda _, token: token if token in {"owner", "other"} else None)
    app = create_app(settings)
    headers = {"Authorization": "Bearer owner"}
    with TestClient(app, base_url=ORIGIN) as first, TestClient(app, base_url=ORIGIN) as second:
        page = first.get("/", headers=headers)
        csrf = re.search(r'<meta name="csrf-token" content="([^"]+)"', page.text)[1]
        created = first.post("/api/v1/orders", headers={
            **headers, "Origin": ORIGIN, "Idempotency-Key": str(uuid4()), "X-CSRF-Token": csrf,
        })
        assert created.status_code == 201
        order_id = created.json()["id"]
        assert second.get("/account").status_code == 401
        assert order_id in second.get("/account", headers=headers).text
        assert second.get(f"/s/coffee/orders/{order_id}", headers=headers).status_code == 200
        other = {"Authorization": "Bearer other"}
        assert second.get(f"/s/coffee/orders/{order_id}", headers=other).status_code == 404
        assert second.post("/account/password/login", data={"token": "none", "username": "someone", "password": PASSWORD},
                           headers=headers).status_code == 404


def test_vk_binds_device_and_echoed_state_and_yandex_uses_profile_id(monkeypatch):
    calls = []

    def exchange(url, **kwargs):
        calls.append((url, kwargs))
        body = {"state": "state", "user_id": 82} if "vk.com" in url else {"access_token": "fixture-access"}
        return httpx.Response(200, json=body, request=httpx.Request("POST", url))

    def profile(url, **kwargs):
        assert url == "https://login.yandex.ru/info?format=json"
        assert kwargs["headers"] == {"Authorization": "OAuth fixture-access"}
        return httpx.Response(200, json={"id": "93", "default_email": "unused@example.test"}, request=httpx.Request("GET", url))

    monkeypatch.setattr("app.social_login.httpx.post", exchange)
    monkeypatch.setattr("app.social_login.httpx.get", profile)
    assert identity_subject("vk", ORIGIN, ["123", ""], "code", "verifier", "", "state", "device") == "82"
    assert calls[0][1]["params"]["device_id"] == "device" and calls[0][1]["data"] == {"code": "code"}
    with pytest.raises(ValueError, match="попытке"):
        identity_subject("vk", ORIGIN, ["123", ""], "code", "verifier", "", "wrong", "device")
    assert identity_subject("yandex", ORIGIN, ["456", "fixture"], "code", "verifier", "", "state", "") == "93"
