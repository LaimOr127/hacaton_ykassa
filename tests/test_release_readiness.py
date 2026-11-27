import json
import time
from uuid import uuid4

import anyio
import httpx
import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from app.agent import DemoAgent, ModelAgent, ModelConnection
from app.catalog import Catalog
from app.gateway import McpPool, Payment, Refund, provider_amount
from app.mail import smtp_connection
from app.main import create_app
from app.merchants import CLEANING_CSV, price_list
from app.settings import Settings
from app.storage import Store
from test_merchants import ORIGIN, OWNER, SETUP, cleaning_order, confirm, form_token, publish, register


class Gateway:
    def __init__(self):
        self.payments = {}
        self.paid = set()
        self.returned = False

    def create(self, amount, attempt_id):
        payment = Payment(
            id=attempt_id, status="pending", amount=provider_amount(amount),
            description=f"Order {attempt_id}", confirmation_url="https://yookassa.ru/checkout/test",
        )
        self.payments[attempt_id] = payment
        return payment

    def get(self, external_id):
        payment = self.payments[external_id]
        return payment.model_copy(update={"status": "succeeded"}) if external_id in self.paid else payment

    def list_refunds(self, payment_id):
        if not self.returned:
            return []
        return [Refund(id="fixture-refund", payment_id=payment_id, status="succeeded",
                       amount=self.payments[payment_id].amount)]


def settings_for(tmp_path, mode="simulated", **overrides):
    return Settings(str(tmp_path / "release.sqlite3"), ORIGIN, mode=mode, setup_key=SETUP, **overrides)


def test_menu_balance_and_terminal_status_do_not_take_write_lock(tmp_path):
    settings = settings_for(tmp_path)
    with TestClient(create_app(settings), base_url=ORIGIN) as client:
        seller = register(client)
        assert publish(client, seller).status_code == 200
        storefront = client.get("/s/cleaning").text
        assert "Прайс магазина" in storefront and "51–70 м²" in storefront and "4\u202f500,00 ₽" in storefront
        headers, order, url = cleaning_order(client)
        deposit = confirm(client, headers, order, url)
        def approve(response):
            path = response.headers["Location"].removeprefix("/api/v1")
            return client.post("/demo" + path + "/approve", headers=headers)

        assert approve(deposit).json()["status"] == "succeeded"
        assert client.get(url, headers=headers).json()["status"] == "deposit_received"
        key = str(uuid4())
        body = {"order_version": order["version"], "balance_payment": True}
        attempt_url = url + "/payment-attempts"
        balance = client.post(attempt_url, headers={**headers, "Idempotency-Key": key}, json=body)
        assert balance.status_code == 201
        assert balance.json()["balance_payment"] is True and balance.json()["amount_kopecks"] == 371000
        assert approve(balance).json()["status"] == "succeeded"
        assert client.get(url, headers=headers).json()["status"] == "paid"
        assert client.post(attempt_url, headers={**headers, "Idempotency-Key": key}, json=body).status_code == 200
        with Store(settings.db_path, "simulated").transaction():
            assert client.get(balance.headers["Location"], headers=headers).json()["status"] == "succeeded"


def test_merchant_checks_pending_and_refund_blocks_fulfillment(tmp_path):
    settings = settings_for(tmp_path, "live")
    gateway = Gateway()
    with TestClient(create_app(settings, agent=DemoAgent(), gateway=gateway), base_url=ORIGIN) as client:
        seller = register(client)
        assert publish(client, seller).status_code == 200
        headers, order, url = cleaning_order(client)
        attempt = confirm(client, headers, order, url).json()
        merchant_url = "/merchant/orders/" + order["id"]
        assert "Проверить оплату" in client.get(merchant_url).text
        gateway.paid.add(attempt["id"])
        data, origin = {"token": seller}, {"Origin": ORIGIN}
        assert client.post(merchant_url + "/payment-review", data=data, headers=origin).status_code == 200
        assert client.get(url, headers=headers).json()["status"] == "deposit_received"
        gateway.returned = True
        response = client.post(merchant_url + "/fulfill", data=data, headers=origin)
        assert response.status_code == 409
        card = client.get(url, headers=headers).json()
        assert card["refunded_kopecks"] == 159000 and not card.get("fulfilled")
        assert "Возвращено покупателю" in client.get(merchant_url).text
        rejected = client.post(url + "/payment-attempts", headers={**headers, "Idempotency-Key": str(uuid4())},
                               json={"order_version": order["version"], "balance_payment": True})
        assert rejected.status_code == 409 and rejected.json()["code"] == "payment_refunded"


def test_background_reconciles_without_buyer_poll(tmp_path, monkeypatch):
    monkeypatch.setattr("app.payments.RECONCILE_INTERVAL", 0.02)
    settings = settings_for(tmp_path, "live")
    gateway = Gateway()
    with TestClient(create_app(settings, agent=DemoAgent(), gateway=gateway), base_url=ORIGIN) as client:
        seller = register(client)
        assert publish(client, seller).status_code == 200
        headers, order, _ = cleaning_order(client)
        attempt = confirm(client, headers, order, f"/api/v1/orders/{order['id']}").json()
        gateway.paid.add(attempt["id"])
        store = Store(settings.db_path, "live")
        with store.transaction() as db:
            db.execute("UPDATE attempts SET next_check = 0 WHERE id = ?", (attempt["id"],))
        for _ in range(120):
            with store.read() as db:
                if db.execute("SELECT state FROM orders WHERE id = ?", (order["id"],)).fetchone()[0] == "deposit_received":
                    return
            time.sleep(0.02)
    pytest.fail("Background reconciliation did not settle the order")


def test_recover_revoked_connector_while_payment_is_pending(tmp_path, monkeypatch):
    original = "fixture-original-token"
    replacement = "fixture-replacement-token"
    provider = Gateway()
    revoked = False

    class Connector:
        def __init__(self, token):
            self.token = token

        def check(self):
            return not (revoked and self.token == original)

        def create(self, *args):
            return provider.create(*args)

        def get(self, external_id):
            if not self.check():
                raise OSError("revoked")
            return provider.get(external_id)

    monkeypatch.setattr("app.main.YooKassa", Connector)
    monkeypatch.setattr("app.merchants.YooKassa", Connector)
    settings = settings_for(tmp_path, "live", vault_key=Fernet.generate_key().decode())
    with TestClient(create_app(settings, agent=DemoAgent()), base_url=ORIGIN) as client:
        seller = register(client)
        assert publish(client, seller).status_code == 200
        origin = {"Origin": ORIGIN}
        connect = "/merchant/connection"
        data = {"token": seller, "mcp_token": original}
        assert client.post(connect, data=data, headers=origin).status_code == 200
        headers, order, url = cleaning_order(client)
        attempt = confirm(client, headers, order, url)
        revoked = True
        provider.paid.add(attempt.json()["id"])
        data.update(mcp_token=replacement, recover=True)
        assert client.post(connect, data=data, headers=origin).status_code == 200
        assert client.get(attempt.headers["Location"], headers=headers).json()["status"] == "succeeded"


def test_owner_code_rotation_revokes_old_sessions(tmp_path):
    settings = settings_for(tmp_path)
    app = create_app(settings)
    headers = {"Origin": ORIGIN}
    with TestClient(app, base_url=ORIGIN) as owner, TestClient(app, base_url=ORIGIN) as employee:
        token = register(owner)

        def login(code):
            page = employee.get("/merchant/login")
            return employee.post("/merchant/login", data={
                "token": form_token(page.text), "slug": "cleaning", "code": code,
            }, headers=headers)

        assert login(OWNER).status_code == 200
        next_code = "fixture-rotated-owner-code"
        result = owner.post("/merchant/access/revoke", data={
            "token": token, "current_code": OWNER, "new_code": next_code,
        }, headers=headers)
        assert result.status_code == 200 and "доступы продавца отозваны" in result.text
        assert employee.get("/merchant", follow_redirects=False).headers["Location"] == "/merchant/login"
        assert login(OWNER).status_code == 401
        assert login(next_code).status_code == 200


def test_chunked_body_size_and_browser_error(tmp_path):
    with TestClient(create_app(settings_for(tmp_path, accounts_enabled=True)), base_url=ORIGIN) as client:
        page = client.get("/")
        token = page.text.split('name="csrf-token" content="', 1)[1].split('"', 1)[0]
        headers = {"Origin": ORIGIN, "X-CSRF-Token": token, "Idempotency-Key": str(uuid4())}
        assert client.post("/api/v1/orders", headers=headers, content=iter((b"{", b"}"))).status_code == 201
        assert client.post("/api/v1/orders", headers=headers, content=iter((b"x" * 15000, b"x" * 5010))).status_code == 413
        error = client.get("/s/coffee/orders/missing")
        assert error.status_code == 404 and "text/html" in error.headers["Content-Type"]
        assert "Заказ недоступен" in error.text


def test_model_receives_priced_tiers_and_accepts_fenced_json():
    catalog = Catalog("cleaning", "Клининг", price_list(CLEANING_CSV), 30)

    def responder(request):
        value = json.loads(request.content)
        prompt = json.loads(value["messages"][1]["content"])
        assert value["max_tokens"] >= 1000 and prompt["deposit_percent"] == 30
        assert prompt["catalog"]["cleaning"]["tiers"][1] == {"area_from": 51, "area_to": 70, "price_kopecks": 450000}
        assert prompt["catalog"]["oven"]["unit_kopecks"] == 80000
        content = json.dumps({"outcome": "clarification", "quantities": {}, "reply": "Уборка: 4 500 ₽."})
        return httpx.Response(200, json={"choices": [{"message": {"content": "```json\n" + content + "\n```"}}]})

    with httpx.Client(transport=httpx.MockTransport(responder)) as client:
        selected = ModelConnection("https://fixture.test", "fixture-model", "fixture-key", client)
        proposal = ModelAgent.request(selected, catalog, {}, [], "Сколько стоит уборка 58 м²?")
    assert proposal.outcome == "clarification" and "4 500" in proposal.reply


def test_shared_mcp_client_keeps_each_request_token_and_omits_cookies(monkeypatch):
    requests = []
    client_type = httpx.AsyncClient

    def reply(request):
        requests.append((request.headers["Authorization"], request.headers.get("Cookie")))
        return httpx.Response(200, headers={"Set-Cookie": "provider-session=discard; Secure"})

    monkeypatch.setattr("app.gateway.httpx.AsyncClient", lambda **options: client_type(
        transport=httpx.MockTransport(reply), **options,
    ))

    async def check():
        pool = McpPool()
        async with pool.open():
            async def request():
                await anyio.sleep(0)
                await pool.client.get("https://mcp.yookassa.ru")
            async with anyio.create_task_group() as batch:
                for token in ("fixture-first", "fixture-second"):
                    batch.start_soon(anyio.to_thread.run_sync, pool.run, token, request)

    anyio.run(check)
    assert sorted(requests) == [("Bearer fixture-first", None), ("Bearer fixture-second", None)]


def test_smtp_587_negotiates_tls_before_auth(tmp_path, monkeypatch):
    steps = []

    class Relay:
        def __init__(self, host, port, *, timeout):
            assert host == "example.test" and port == 587 and timeout > 0

        def __enter__(self):
            return self

        def __exit__(self, *_):
            pass

        def starttls(self, *, context):
            assert context.check_hostname
            steps.append("tls")

        def login(self, user, password):
            assert user == "fixture-user" and password == "fixture-password"
            steps.append("login")

    monkeypatch.setattr("app.mail.smtplib.SMTP", Relay)
    settings = settings_for(tmp_path, "live", mail_host="example.test", mail_port=587,
                            mail_user="fixture-user", mail_password="fixture-password")
    with smtp_connection(settings):
        steps.append("ready")
    assert steps == ["tls", "login", "ready"]
