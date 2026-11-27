import re
import sqlite3
import time
from dataclasses import replace
from types import SimpleNamespace
from uuid import uuid4

import httpx
import jwt
import pytest
from cryptography.fernet import Fernet
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient

from app.agent import Proposal
from app.catalog import COFFEE
from app.main import create_app
from app.merchants import Merchants
from app.settings import Settings
from app.storage import Store
from test_merchants import register, start

ORIGIN = "https://orders.company.test"
ISSUER = "https://identity.company.test/realms/test"
AUDIENCE = "orders-web"


def test_private_requires_verified_identity_for_ui_api_and_old_cookie(tmp_path, monkeypatch):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    monkeypatch.setattr(jwt.PyJWKClient, "get_signing_key_from_jwt", lambda *_: SimpleNamespace(key=key.public_key()))
    settings = Settings(
        str(tmp_path / "private.sqlite3"), ORIGIN, access="private",
        oidc_issuer=ISSUER, oidc_jwks=f"{ISSUER}/jwks", oidc_client_id=AUDIENCE,
        oidc_email_domain="company.test",
        oidc_merchants={"seller": "coffee"},
    )

    def identity(subject="seller", **overrides):
        now = int(time.time())
        token = jwt.encode(
            {"sub": subject, "iss": ISSUER, "aud": AUDIENCE, "iat": now, "exp": now + 60,
             "email": f"{subject}@company.test", "email_verified": True, **overrides},
            key, algorithm="RS256",
        )
        return {"Authorization": "Bearer " + token, "Origin": ORIGIN}

    with TestClient(create_app(settings), base_url=ORIGIN) as client:
        assert client.get("/healthz").status_code == 200
        for path in ("/", "/merchant", "/merchant/login", "/api/v1/orders/sample", "/static/order.js"):
            assert client.get(path, headers={"X-Forwarded-User": "seller", "X-Auth-Request-User": "seller"}).status_code == 401
        for forged in ({"iss": "https://unknown.test"}, {"aud": "other"}, {"exp": 1}, {"aud": ["other", AUDIENCE]},
                       {"email": "seller@foreign.test"}, {"email_verified": False}):
            assert client.get("/", headers=identity(**forged)).status_code == 401
        wrong_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        now = int(time.time())
        bad_signature = jwt.encode(
            {"sub": "seller", "iss": ISSUER, "aud": AUDIENCE, "iat": now, "exp": now + 60}, wrong_key, algorithm="RS256",
        )
        assert client.get("/", headers={"Authorization": "Bearer " + bad_signature}).status_code == 401
        assert client.get("/merchant", headers=identity("unassigned")).status_code == 403
        seller = identity()
        activation = client.get("/merchant/login", headers=seller)
        csrf = re.search(r'name="token" value="([^"]+)"', activation.text)[1]
        assert client.post("/merchant/login", data={"token": csrf, "slug": "coffee", "code": "legacy"}, headers=seller).status_code == 403
        assert client.post("/merchant/register", data={"token": csrf, "slug": "another", "name": "Other"}, headers=seller).status_code == 403
        assert client.post("/merchant/register", data={"token": csrf, "slug": "coffee", "name": "Cafe"}, headers=seller).status_code == 200
        assert "__Host-merchant_session" not in client.cookies
        page = client.get("/", headers=seller)
        buyer_csrf = re.search(r'<meta name="csrf-token" content="([^"]+)"', page.text)[1]
        order = client.post("/api/v1/orders", headers={**seller, "Idempotency-Key": str(uuid4()), "X-CSRF-Token": buyer_csrf})
        assert order.status_code == 201
        order_url = order.headers["Location"]
        assert client.get(order_url, headers=identity("another-user")).status_code == 401
        client.get("/", headers=identity("another-user"))
        assert client.get(order_url, headers=identity("another-user")).status_code == 404
        assert client.get(order_url).status_code == 401


def test_saved_model_uses_only_selected_tenant_and_endpoint(tmp_path, monkeypatch):
    settings = Settings(
        str(tmp_path / "model.sqlite3"), "https://example.test", mode="live",
        setup_key="fixture-setup-code-2026-only", vault_key=Fernet.generate_key().decode(),
        llm_key="global-fallback-key", llm_url="https://gateway.company.test/v1",
    )
    checked = []

    def probe(connection, catalog, quantities, recent, text):
        checked.append(connection)
        return Proposal(outcome="clarification", quantities={}, reply="Проверка")

    monkeypatch.setattr("app.agent.ModelAgent.request", staticmethod(probe))
    listed = []

    def list_models(url, *, headers, timeout, follow_redirects):
        listed.append((url, headers["Authorization"]))
        assert timeout == 6 and follow_redirects is False
        return httpx.Response(
            200, json={"data": [{"id": "test-model"}, {"id": "other/model"}, {"id": "with spaces"}]},
            request=httpx.Request("GET", url),
        )

    monkeypatch.setattr("app.merchants.httpx.get", list_models)
    with TestClient(create_app(settings), base_url=settings.origin) as client:
        csrf = register(client, "clinic")
        url = "/merchant/model"
        headers = {"Origin": settings.origin}
        key_value = "fixture-company-api-key"
        body = {"token": csrf, "model": "test-model", "api_key": key_value}
        options = url + "/options"
        assert client.post(options, data={**body, "provider": "https://unknown.test"}, headers=headers).status_code == 400
        assert not listed
        response = client.post(options, data={**body, "provider": "company"}, headers=headers)
        assert response.json() == {"models": ["other/model", "test-model"]}
        assert listed == [(settings.llm_url + "/models", "Bearer " + key_value)]
        assert client.post(url, data={**body, "provider": "https://unknown.test"}, headers=headers).status_code == 400
        assert not checked
        accepted = client.post(url, data={**body, "provider": "company"}, headers=headers)
        assert accepted.status_code == 200 and key_value not in accepted.text
        assert len(checked) == 1 and checked[0].url == settings.llm_url and checked[0].key == key_value

    store = Store(settings.db_path, "live")
    assert Merchants(store, settings, COFFEE).model_connection("clinic").key == key_value
    assert Merchants(store, replace(settings, llm_url="https://different.test/v1"), COFFEE).model_connection("clinic") is None
    with store.transaction() as db:
        ciphertext = db.execute("SELECT ciphertext FROM model_connectors WHERE merchant='clinic'").fetchone()[0]
        assert key_value not in ciphertext
        db.execute("UPDATE model_connectors SET ciphertext='broken' WHERE merchant='clinic'")
    assert Merchants(store, settings, COFFEE).model_connection("clinic") is None


def test_disabling_model_blocks_global_fallback(tmp_path):
    settings = Settings(
        str(tmp_path / "disabled.sqlite3"), "https://example.test", mode="live",
        setup_key="fixture-setup-code-2026-only", default_owner_code="fixture-owner-code",
        llm_key="fixture-global-key",
    )
    merchants = Merchants(Store(settings.db_path, "live"), settings, COFFEE)
    with TestClient(create_app(settings), base_url=settings.origin) as client:
        csrf = register(client, "coffee")
        assert merchants.model_connection("coffee").key == settings.llm_key
        assert "Отключить модель" in client.get("/merchant").text
        response = client.post("/merchant/model/disconnect", data={"token": csrf}, headers={"Origin": settings.origin})
        assert response.status_code == 200
        assert merchants.model_connection("coffee") is None


def test_slow_model_probe_cannot_undo_an_existing_disconnect(tmp_path, monkeypatch):
    settings = Settings(
        str(tmp_path / "model-race.sqlite3"), "https://example.test", mode="live",
        setup_key="fixture-setup-code-2026-only", vault_key=Fernet.generate_key().decode(),
    )
    merchants = Merchants(Store(settings.db_path, "live"), settings, COFFEE)

    def probe(*_):
        merchants.disconnect_model("clinic")
        return Proposal(outcome="clarification", quantities={}, reply="Проверка")

    monkeypatch.setattr("app.agent.ModelAgent.request", staticmethod(probe))
    with TestClient(create_app(settings), base_url=settings.origin) as client:
        csrf = register(client, "clinic")
        merchants.disconnect_model("clinic")
        response = client.post("/merchant/model", data={
            "token": csrf, "provider": "company", "model": "fixture-model", "api_key": "fixture-api-key",
        }, headers={"Origin": settings.origin})
        assert response.status_code == 409
        assert merchants.model_summary("clinic")["provider"] == "disabled"
        assert merchants.model_connection("clinic") is None


def test_missing_model_does_not_consume_model_quota(tmp_path, monkeypatch):
    settings = Settings(
        str(tmp_path / "quota.sqlite3"), "https://example.test", mode="live",
        setup_key="fixture-setup-code-2026-only", default_owner_code="fixture-owner-code",
        vault_key=Fernet.generate_key().decode(),
        model_session_limit=1, model_merchant_limit=1, model_instance_limit=1,
    )
    monkeypatch.setattr("app.agent.ModelAgent.request", staticmethod(
        lambda *_: Proposal(outcome="clarification", quantities={}, reply="Проверка"),
    ))
    with TestClient(create_app(settings), base_url=settings.origin) as client:
        csrf = register(client, "coffee")
        headers, order = start(client, "coffee")
        url = "/api/v1/orders/" + order["id"] + "/messages"
        message = {"client_message_id": str(uuid4()), "order_version": order["version"], "text": "Есть кофе?"}
        assert client.post(url, json=message, headers=headers).status_code == 503
        response = client.post("/merchant/model", data={
            "token": csrf, "provider": "openai", "model": "fixture-model", "api_key": "fixture-api-key",
        }, headers={"Origin": settings.origin})
        assert response.status_code == 200
        assert client.post(url, json=message, headers=headers).status_code == 200
        assert client.post(url, json={**message, "client_message_id": str(uuid4())}, headers=headers).status_code == 429


@pytest.mark.parametrize(("provider_failure", "retry_status"), [
    (httpx.ConnectError, 200),
    (httpx.ConnectTimeout, 200),
    (httpx.ReadTimeout, 429),
])
def test_model_quota_retains_only_attempts_that_could_reach_provider(tmp_path, monkeypatch, provider_failure, retry_status):
    settings = Settings(
        str(tmp_path / "network-quota.sqlite3"), "https://example.test", mode="live", llm_key="fixture-key",
        model_session_limit=1, model_merchant_limit=1, model_instance_limit=1,
    )
    failures = iter([provider_failure("fixture failure")])

    def probe(*_):
        if error := next(failures, None):
            raise error
        return Proposal(outcome="clarification", quantities={}, reply="Проверка")

    monkeypatch.setattr("app.agent.ModelAgent.request", staticmethod(probe))
    with TestClient(create_app(settings), base_url=settings.origin) as client:
        headers, order = start(client, "coffee")
        url = "/api/v1/orders/" + order["id"] + "/messages"
        message = {"client_message_id": str(uuid4()), "order_version": order["version"], "text": "Есть кофе?"}
        assert client.post(url, json=message, headers=headers).status_code == 503
        assert client.post(url, json=message, headers=headers).status_code == retry_status


def test_database_lock_returns_retryable_http_status(tmp_path):
    settings = Settings(str(tmp_path / "busy.sqlite3"), "https://example.test")
    with TestClient(create_app(settings), base_url=settings.origin) as client:
        blocker = sqlite3.connect(settings.db_path)
        try:
            blocker.execute("BEGIN IMMEDIATE")
            response = client.get("/")
            assert response.status_code == 503
            assert response.json()["code"] == "storage_busy"
            assert response.headers["Retry-After"] == "2"
        finally:
            blocker.close()
        assert client.get("/").status_code == 200
