import re
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from threading import Event
from uuid import uuid4

import pytest
import yaml
from fastapi.testclient import TestClient
from jsonschema import Draft202012Validator
from referencing import Registry, Resource

from app.catalog import COFFEE, Product
from app.gateway import Payment, PaymentNotSent, ProviderAmount, amount_value
from app.main import create_app
from app.settings import Settings

ORIGIN = "https://example.test"
ORDERS = "/api/v1/orders"
START = "20 завтраков, из них 5 вегетарианских"
CHANGE = "Замените два обычных завтрака на вегетарианские."
CONTRACT = yaml.safe_load(Path("contracts/openapi.yaml").read_text(encoding="utf-8"))
CONTRACT["$schema"] = "https://json-schema.org/draft/2020-12/schema"
REGISTRY = Registry().with_resource("urn:contract", Resource.from_contents(CONTRACT))


def validate(schema, value):
    Draft202012Validator(
        {"$ref": f"urn:contract#/components/schemas/{schema}"}, registry=REGISTRY,
    ).validate(value)


def test_actual_http_operations_match_contract(settings):
    app_schema = create_app(settings).openapi()
    methods = {"get", "post", "put", "patch", "delete"}
    actual = {
        path.removeprefix("/api/v1"): set(operations) & methods
        for path, operations in app_schema["paths"].items() if path.startswith("/api/v1/")
    }
    expected = {
        path.replace("{orderId}", "{order_id}").replace("{attemptId}", "{attempt_id}"): set(operations) & methods
        for path, operations in CONTRACT["paths"].items()
    }
    assert actual == expected


def csrf_headers(client):
    page = client.get("/")
    assert page.status_code == 200
    token = re.search(r'<meta name="csrf-token" content="([^"]+)"', page.text).group(1)
    return {"X-CSRF-Token": token, "Origin": ORIGIN}


def key(headers, identifier=None):
    return {**headers, "Idempotency-Key": identifier or str(uuid4())}


def message(client, headers, order, text, message_id=None):
    return client.post(
        f"{ORDERS}/{order['id']}/messages", headers=headers,
        json={"order_version": order["version"], "client_message_id": message_id or str(uuid4()), "text": text},
    )


def confirm(client, headers, order):
    return client.post(
        f"{ORDERS}/{order['id']}/payment-attempts", headers=headers, json={"order_version": order["version"]},
    )


def storefront(client):
    headers = csrf_headers(client)
    resp = client.post(ORDERS, headers=key(headers))
    assert resp.status_code == 201
    assert resp.headers["cache-control"] == "no-store"
    order = resp.json()
    validate("Order", order)
    return headers, order


@pytest.fixture
def settings(tmp_path):
    return Settings(str(tmp_path / "test.sqlite3"), ORIGIN)


def test_exact_amount_replays_and_restarts(settings):
    with TestClient(create_app(settings), base_url=ORIGIN) as client:
        headers = csrf_headers(client)
        create_headers = key(headers)
        order = client.post(ORDERS, headers=create_headers).json()
        resp = message(client, headers, order, START)
        start = resp.json()["order"]
        assert start["total_kopecks"] == 725_000
        message_id = str(uuid4())
        changed = message(client, headers, start, CHANGE, message_id)
        validate("MessageResult", changed.json())
        current = changed.json()["order"]
        assert [item["quantity"] for item in current["items"]] == [13, 7]
        assert current["total_kopecks"] == 735_000
        assert message(client, headers, start, CHANGE, message_id).json() == changed.json()
        assert message(client, headers, start, "Другое сообщение", message_id).json()["code"] == "idempotency_conflict"
        assert client.post(ORDERS, headers=create_headers).json() == order
        assert confirm(client, key(headers), start).json()["code"] == "stale_version"
        pay_headers = key(headers)
        first = confirm(client, pay_headers, current)
        assert first.status_code == 201
        validate("PaymentAttempt", first.json())
        assert first.json()["amount_kopecks"] == 735_000
        assert confirm(client, pay_headers, current).status_code == 200
        assert confirm(client, key(headers), current).json()["code"] == "payment_in_progress"
        assert message(client, headers, current, CHANGE).json()["code"] == "order_locked"
        attempt_path = first.headers["location"]
        cookies = dict(client.cookies)

    with TestClient(create_app(settings), base_url=ORIGIN, cookies=cookies) as client:
        assert client.get(attempt_path).json()["status"] == "pending"
        simulation_path = "/demo" + attempt_path.removeprefix("/api/v1") + "/approve"
        assert client.post(simulation_path, headers=headers, json={}).json()["status"] == "succeeded"
        assert client.get(attempt_path).json()["status"] == "succeeded"
        assert client.get(f"{ORDERS}/{current['id']}").json()["status"] == "paid"
        assert confirm(client, pay_headers, current).json()["status"] == "succeeded"
        assert confirm(client, key(headers), current).json()["code"] == "order_not_payable"


def test_origin_sessions_and_untrusted_amount(settings):
    with TestClient(create_app(settings), base_url=ORIGIN) as owner, TestClient(
        owner.app, base_url=ORIGIN,
    ) as other:
        assert owner.post(ORDERS, headers=key({})).status_code == 401
        headers, order = storefront(owner)
        cookie = owner.cookies.get("__Host-customer_session")
        assert cookie and len(cookie) == 43
        assert owner.post(ORDERS, headers=key({**headers, "Origin": "https://attacker.test"})).status_code == 403
        assert owner.post(ORDERS, headers=key({"Origin": ORIGIN})).status_code == 403
        assert owner.post(f"{ORDERS}/{order['id']}/payment-attempts", headers=key(headers),
                          json={"order_version": 1, "amount_kopecks": 1}).status_code == 400
        current = message(owner, headers, order, START).json()["order"]
        attempt_path = confirm(owner, key(headers), current).headers["location"]
        other_headers = csrf_headers(other)
        assert other.get(f"{ORDERS}/{order['id']}").status_code == 404
        assert other.get(attempt_path).status_code == 404
        assert confirm(other, key(other_headers), current).status_code == 404


def test_price_change_requires_new_confirmation(settings):
    with TestClient(create_app(settings), base_url=ORIGIN) as client:
        headers, order = storefront(client)
        current = message(client, headers, order, START).json()["order"]
        cookies = dict(client.cookies)
    revised = replace(COFFEE, products={**COFFEE.products, "breakfast": Product("Обычный завтрак", 40_000)})
    with TestClient(create_app(settings, catalog=revised), base_url=ORIGIN, cookies=cookies) as client:
        rejected = confirm(client, key(headers), current)
        assert rejected.status_code == 409
        assert rejected.json()["current_version"] == current["version"] + 1
        refreshed = client.get(f"{ORDERS}/{current['id']}").json()
        assert refreshed["total_kopecks"] == 800_000
        assert refreshed["payment_attempt_id"] is None


class BrokenGateway:
    def __init__(self):
        self.calls = 0

    def create(self, amount, attempt_id):
        self.calls += 1
        raise TimeoutError("Ответ потерян")


def test_indeterminate_result_blocks_new_payment_after_restart(settings):
    gateway = BrokenGateway()
    with TestClient(create_app(settings, gateway=gateway), base_url=ORIGIN) as client:
        headers, order = storefront(client)
        current = message(client, headers, order, START).json()["order"]
        first = confirm(client, key(headers), current)
        assert first.json()["status"] == "unknown"
        cookies = dict(client.cookies)
    with TestClient(create_app(settings, gateway=gateway), base_url=ORIGIN, cookies=cookies) as client:
        assert client.get(first.headers["location"]).json()["status"] == "unknown"
        assert confirm(client, key(headers), current).json()["code"] == "payment_in_progress"
        assert gateway.calls == 1


def test_parallel_confirmation_creates_one_provider_payment(settings):
    entered, release = Event(), Event()

    class SlowGateway:
        calls = 0

        def create(self, amount, attempt_id):
            self.calls += 1
            entered.set()
            assert release.wait(10)
            return Payment(id="provider", status="pending", amount=ProviderAmount(value=amount_value(amount), currency="RUB"))

    gateway = SlowGateway()
    with TestClient(create_app(settings, gateway=gateway), base_url=ORIGIN) as client:
        headers, order = storefront(client)
        current = message(client, headers, order, START).json()["order"]
        with ThreadPoolExecutor() as pool:
            first = pool.submit(confirm, client, key(headers), current)
            try:
                assert entered.wait(10)
                second = pool.submit(confirm, client, key(headers), current).result(timeout=5)
                assert second.json()["code"] == "payment_in_progress"
            finally:
                release.set()
            assert first.result(timeout=5).json()["status"] == "pending"
        assert gateway.calls == 1


def test_small_scope_catalog_can_charge_deposit(settings):
    catalog = replace(COFFEE, deposit_percent=30)
    with TestClient(create_app(settings, catalog=catalog), base_url=ORIGIN) as client:
        headers, order = storefront(client)
        current = message(client, headers, order, START).json()["order"]
        assert (current["total_kopecks"], current["payable_kopecks"], current["balance_kopecks"]) == (
            725_000, 217_500, 507_500,
        )
        location = confirm(client, key(headers), current).headers["location"]
        assert client.post("/demo" + location.removeprefix("/api/v1") + "/approve", headers=headers, json={}).status_code == 200
        assert client.get(f"{ORDERS}/{current['id']}").json()["status"] == "deposit_received"


@pytest.mark.parametrize(
    ("created_status", "created_amount", "checked_currency", "expected"),
    [
        ("succeeded", 725_000, "RUB", "canceled"),
        ("pending", 700_000, "RUB", "succeeded"),
        ("pending", 725_000, "USD", "unknown"),
    ],
)
def test_reconcile_create_and_get_disagreements(settings, created_status, created_amount, checked_currency, expected):
    class Gateway:
        reads = 0

        def create(self, amount, attempt_id):
            return Payment(
                id="provider-1", status=created_status,
                amount=ProviderAmount(value=amount_value(created_amount), currency="RUB"),
                confirmation_url="https://yookassa.ru/checkout/test",
            )

        def get(self, external_id):
            self.reads += 1
            assert external_id == "provider-1"
            return Payment(
                id=external_id, status="canceled" if expected == "canceled" else "succeeded",
                amount=ProviderAmount(value="7250.00", currency=checked_currency),
            )

    gateway = Gateway()
    with TestClient(create_app(settings, gateway=gateway), base_url=ORIGIN) as client:
        headers, order = storefront(client)
        current = message(client, headers, order, START).json()["order"]
        confirmed = confirm(client, key(headers), current)
        assert confirmed.json()["status"] in {"pending", "unknown"}
        assert client.get(f"{ORDERS}/{current['id']}").json()["status"] == "payment_pending"
        result = client.get(confirmed.headers["location"]).json()
        assert gateway.reads == 1
        assert result["status"] == expected
        assert "confirmation_url" not in result
        statuses = {"canceled": "agreed", "succeeded": "paid", "unknown": "payment_pending"}
        assert client.get(f"{ORDERS}/{current['id']}").json()["status"] == statuses[expected]


def test_failed_before_remote_create_permits_another_confirmation(settings):
    class RefusedGateway:
        calls = 0

        def create(self, amount, attempt_id):
            self.calls += 1
            raise PaymentNotSent()

    gateway = RefusedGateway()
    with TestClient(create_app(settings, gateway=gateway), base_url=ORIGIN) as client:
        headers, order = storefront(client)
        current = message(client, headers, order, START).json()["order"]
        assert confirm(client, key(headers), current).json()["status"] == "failed"
        assert confirm(client, key(headers), current).json()["status"] == "failed"
        assert gateway.calls == 2


def test_demo_preserves_unknown_items(settings):
    with TestClient(create_app(settings), base_url=ORIGIN) as client:
        headers, order = storefront(client)
        result = message(client, headers, order, START + ", и один сок").json()
        assert result["outcome"] == "clarification"
        assert result["order"] == order
