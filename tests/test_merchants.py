import re
from dataclasses import replace
from uuid import uuid4

import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from app.agent import DemoAgent
from app.catalog import COFFEE
from app.gateway import Payment, ProviderAmount, amount_value
from app.main import create_app
from app.merchants import CLEANING_CSV, Merchants, price_list
from app.settings import Settings
from app.storage import Store

ORIGIN = "https://example.test"
SETUP = "fixture-setup-code-2026-only"
OWNER = "fixture-owner-code"
ORDERS = "/api/v1/orders"


def form_token(html):
    return re.search('name="token" value="([^"]+)"', html).group(1)


def register(client, slug="cleaning"):
    entry = client.get("/merchant/login")
    response = client.post("/merchant/register", data={
        "token": form_token(entry.text), "setup": SETUP, "name": "Чистый дом" if slug == "cleaning" else slug,
        "slug": slug, "code": OWNER,
    }, headers={"Origin": ORIGIN})
    assert response.status_code == 200
    return form_token(response.text)


def publish(client, token, price=CLEANING_CSV, deposit=30, version=None):
    if version is None:
        html = client.get("/merchant").text
        match = re.search(r'name="catalog_version" value="(\d+)"', html)
        version = int(match.group(1)) if match else 0
    return client.post(
        "/merchant/catalog", data={"token": token, "catalog_csv": price, "deposit": str(deposit), "catalog_version": version},
        headers={"Origin": ORIGIN},
    )


def start(client, slug):
    page = client.get("/s/" + slug)
    assert page.status_code == 200
    token = re.search(r'<meta name="csrf-token" content="([^"]+)"', page.text).group(1)
    headers = {"X-Shop": slug, "Origin": ORIGIN, "X-CSRF-Token": token}
    response = client.post(ORDERS, headers={**headers, "Idempotency-Key": str(uuid4())}, json={})
    assert response.status_code == 201
    return headers, response.json()


def cleaning_order(client, slug="cleaning"):
    headers, order = start(client, slug)
    path = f"{ORDERS}/{order['id']}"
    response = client.post(path + "/messages", headers=headers, json={
        "order_version": order["version"], "text": "Нужна уборка квартиры 58 м² и чистка духовки.",
        "client_message_id": str(uuid4()),
    })
    assert response.status_code == 200
    order = response.json()["order"]
    assert (order["total_kopecks"], order["payable_kopecks"], order["balance_kopecks"]) == (530_000, 159_000, 371_000)
    return headers, order, path


def confirm(client, headers, order, path):
    return client.post(path + "/payment-attempts", headers={**headers, "Idempotency-Key": str(uuid4())},
                       json={"order_version": order["version"]})


@pytest.fixture
def settings(tmp_path):
    return Settings(str(tmp_path / "merchants.sqlite3"), ORIGIN, setup_key=SETUP, default_owner_code=OWNER)


def test_onboard_two_merchants_and_keep_buyer_sessions_isolated(settings):
    app = create_app(settings)
    with TestClient(app, base_url=ORIGIN) as seller:
        token = register(seller)
        assert seller.get("/s/cleaning").status_code == 404
        assert publish(seller, token).status_code == 200
        with TestClient(app, base_url=ORIGIN) as buyer:
            headers, order, path = cleaning_order(buyer)
            coffee, coffee_order = start(buyer, "coffee")
            assert buyer.get(path, headers=coffee).status_code == 404
            assert buyer.get(f"{ORDERS}/{coffee_order['id']}", headers=headers).status_code == 404
            assert buyer.get(path, headers=headers).status_code == 200
            with Store(settings.db_path, "simulated").transaction() as db:
                db.execute("UPDATE sessions SET expires_at = 0 WHERE merchant = 'coffee'")
            assert buyer.get("/s/coffee").status_code == 200
            assert buyer.get(path, headers=headers).status_code == 200
            attempt = confirm(buyer, headers, order, path)
            location = attempt.headers["location"]
            assert buyer.get(location, headers=coffee).status_code == 404
            approval = buyer.post("/demo" + location.removeprefix("/api/v1") + "/approve", headers=headers, json={})
            assert approval.json()["status"] == "succeeded"
        page = seller.get("/merchant")
        assert "Предоплата получена" in page.text
        assert order["id"][:8] in page.text
        register(seller, "coffee")
        page = seller.get("/merchant")
        assert order["id"][:8] not in page.text
        assert coffee_order["id"][:8] in page.text


def test_catalog_publish_is_atomic_and_forces_reconfirmation(settings):
    with TestClient(create_app(settings), base_url=ORIGIN) as client:
        token = register(client)
        assert publish(client, token).status_code == 200
        headers, order, path = cleaning_order(client)
        invalid = CLEANING_CSV.replace("51,70", "50,70")
        response = publish(client, token, invalid)
        assert response.status_code == 400
        assert "пересекаются" in response.text
        assert "590" in client.get(path, headers=headers).text
        assert publish(client, token, CLEANING_CSV.replace("450000", "550000")).status_code == 200
        outdated = publish(client, token, CLEANING_CSV, 40, version=1)
        assert outdated.status_code == 409
        assert "Прайс изменился" in outdated.text and 'value="40"' in outdated.text
        assert confirm(client, headers, order, path).json()["code"] == "stale_version"
        revised = client.get(path, headers=headers).json()
        assert revised["payable_kopecks"] == 189_000
        assert revised["payment_attempt_id"] is None


def test_merchant_posts_require_scope_and_origin(settings):
    with TestClient(create_app(settings), base_url=ORIGIN) as client:
        assert publish(client, "fake").status_code == 401
        token = register(client)
        assert publish(client, "fake").status_code == 403
        spoofed = client.post("/merchant/logout", data={"token": token}, headers={"Origin": "https://wrong.test"})
        assert spoofed.status_code == 403
        assert client.get("/merchant").status_code == 200
        assert client.post("/merchant/logout", data={"token": token}, headers={"Origin": ORIGIN}).status_code == 200
        assert publish(client, token).status_code == 401
        entry = client.get("/merchant/login")
        rejected = client.post("/merchant/register", data={
            "token": form_token(entry.text), "setup": SETUP, "name": "Чужой магазин",
            "slug": "coffee", "code": "different-owner-code",
        }, headers={"Origin": ORIGIN})
        assert rejected.status_code == 400
        assert "зарезервирован" in rejected.text


def test_each_merchant_uses_own_sealed_token(settings, monkeypatch):
    test_settings = replace(
        settings, mode="live", llm_key="fixture", mcp_token="fixture-token-for-coffee",
        vault_key=Fernet.generate_key().decode(),
    )
    calls = []

    class Gateway:
        def __init__(self, token):
            self.token = token

        def check(self):
            return self.token.startswith("fixture-token-for-")

        def create(self, amount, attempt_id):
            calls.append((self.token, amount))
            return Payment(
                id=attempt_id, status="pending", amount=ProviderAmount(value=amount_value(amount), currency="RUB"),
                confirmation_url="https://yookassa.ru/checkout/test",
            )

    monkeypatch.setattr("app.main.YooKassa", Gateway)
    monkeypatch.setattr("app.merchants.YooKassa", Gateway)
    token_value = "fixture-token-for-cleaning"
    with TestClient(create_app(test_settings, agent=DemoAgent()), base_url=ORIGIN) as client:
        token = register(client)
        assert publish(client, token).status_code == 200
        headers, order, path = cleaning_order(client)
        assert confirm(client, headers, order, path).status_code == 503
        invalid = client.post(
            "/merchant/connection",
            data={"token": token, "mcp_token": "fixture-without-tools"},
            headers={"Origin": ORIGIN},
        )
        assert invalid.status_code == 400
        assert "fixture-without-tools" not in invalid.text
        response = client.post("/merchant/connection", data={"token": token, "mcp_token": token_value}, headers={"Origin": ORIGIN})
        assert token_value not in response.text
        assert response.status_code == 200
        assert confirm(client, headers, order, path).json()["status"] == "pending"
        assert calls == [(token_value, 159_000)]
        with Store(test_settings.db_path, "live").read() as db:
            encrypted = db.execute("SELECT mcp_ciphertext FROM merchants WHERE slug='cleaning'").fetchone()[0]
            assert token_value not in encrypted
        token = register(client, "second-cleaning")
        for duplicate in (token_value, test_settings.mcp_token):
            response = client.post(
                "/merchant/connection", data={"token": token, "mcp_token": duplicate}, headers={"Origin": ORIGIN},
            )
            assert response.status_code == 400 and "другим магазином" in response.text


def test_rotate_payment_token_only_after_verifying_store_payment(settings, monkeypatch):
    settings = replace(settings, mode="live", vault_key=Fernet.generate_key().decode())
    original = "fixture-original-connector"
    replacement = "fixture-replacement-connector"
    payments = {}
    settled_ids = set()
    during_verification = None

    class Gateway:
        def __init__(self, token):
            self.token = token

        def check(self):
            return True

        def create(self, amount, attempt_id):
            assert self.token == original
            payment = Payment(
                id=attempt_id, status="pending", amount=ProviderAmount(value=amount_value(amount), currency="RUB"),
                confirmation_url="https://yookassa.ru/checkout/test", description=f"Order {attempt_id}",
            )
            payments[attempt_id] = payment
            return payment

        def get(self, external_id):
            payment = payments[external_id]
            if self.token == "fixture-mismatched-connector":
                return payment.model_copy(update={"description": "Order another-attempt"})
            if self.token == "fixture-racing-connector":
                during_verification()
            return payment.model_copy(update={"status": "succeeded"}) if external_id in settled_ids else payment

    monkeypatch.setattr("app.main.YooKassa", Gateway)
    monkeypatch.setattr("app.merchants.YooKassa", Gateway)
    with TestClient(create_app(settings, agent=DemoAgent()), base_url=ORIGIN) as client:
        csrf = register(client)
        assert publish(client, csrf).status_code == 200

        def connect(value):
            return client.post("/merchant/connection", data={"token": csrf, "mcp_token": value}, headers={"Origin": ORIGIN})

        assert connect(original).status_code == 200
        assert connect(replacement).status_code == 400
        headers, order, url = cleaning_order(client)
        attempt = confirm(client, headers, order, url)
        assert attempt.json()["status"] == "pending"
        active = connect(replacement)
        assert active.status_code == 400 and "активных платежей" in active.text
        settled_ids.add(attempt.json()["id"])
        assert client.get(attempt.headers["Location"], headers=headers).json()["status"] == "succeeded"
        rejected = connect("fixture-mismatched-connector")
        assert rejected.status_code == 400 and "не подтвердил" in rejected.text
        merchants = Merchants(Store(settings.db_path, "live"), settings, COFFEE)
        assert merchants.token("cleaning") == original

        next_headers, next_order, next_url = cleaning_order(client)
        next_attempt = None

        def reserve_during_verification():
            nonlocal next_attempt
            next_attempt = confirm(client, next_headers, next_order, next_url)
            assert next_attempt.json()["status"] == "pending"

        during_verification = reserve_during_verification
        with pytest.raises(ValueError, match="активных платежей"):
            merchants.connect("cleaning", "fixture-racing-connector")
        assert merchants.token("cleaning") == original
        assert next_attempt is not None
        settled_ids.add(next_attempt.json()["id"])
        assert client.get(next_attempt.headers["Location"], headers=next_headers).json()["status"] == "succeeded"

        assert connect(replacement).status_code == 200
        assert merchants.token("cleaning") == replacement


def test_existing_payments_require_verification_after_removing_environment_token(settings, monkeypatch):
    class Gateway:
        def __init__(self, token):
            self.token = token

        def check(self):
            return True

        def create(self, amount, attempt_id):
            return Payment(
                id=attempt_id, status="pending", amount=ProviderAmount(value=amount_value(amount), currency="RUB"),
                confirmation_url="https://yookassa.ru/checkout/test", description=f"Order {attempt_id}",
            )

        def get(self, external_id):
            assert self.token == original
            return Payment(
                id=external_id, status="succeeded", amount=ProviderAmount(value="7250.00", currency="RUB"),
                description=f"Order {external_id}",
            )

    monkeypatch.setattr("app.main.YooKassa", Gateway)
    monkeypatch.setattr("app.merchants.YooKassa", Gateway)
    original = "fixture-original-env-connector"
    settings = replace(settings, mode="live", vault_key=Fernet.generate_key().decode(), mcp_token=original)
    with TestClient(create_app(settings, agent=DemoAgent()), base_url=ORIGIN) as client:
        register(client, "coffee")
        headers, order = start(client, "coffee")
        url = f"{ORDERS}/{order['id']}"
        response = client.post(url + "/messages", headers=headers, json={
            "order_version": order["version"], "text": "20 завтраков, из них 5 вегетарианских",
            "client_message_id": str(uuid4()),
        })
        attempt = confirm(client, headers, response.json()["order"], url)
        assert client.get(attempt.headers["Location"], headers=headers).json()["status"] == "succeeded"

    without_env_token = replace(settings, mcp_token="")
    merchants = Merchants(Store(settings.db_path, "live"), without_env_token, COFFEE)
    with pytest.raises(ValueError, match="Не удалось проверить токен"):
        merchants.connect("coffee", "fixture-unrelated-connector")
    assert merchants.token("coffee") is None


def test_area_bounds_and_csv_validation():
    area = price_list(CLEANING_CSV)["cleaning"]
    assert area.item("cleaning", 50)["amount_kopecks"] == 350_000
    assert area.item("cleaning", 51)["amount_kopecks"] == 450_000
    with pytest.raises(ValueError, match="нет тарифа"):
        area.item("cleaning", 201)
