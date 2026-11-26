import json
from concurrent.futures import ThreadPoolExecutor
from threading import Event
from uuid import uuid4

import httpx
from fastapi.testclient import TestClient

from app.agent import DemoAgent
from app.main import create_app
from app.settings import Settings
from app.storage import Store
from test_checkout import CHANGE, ORIGIN, START, confirm, key, message, storefront, validate
from test_merchants import OWNER, SETUP, register


def test_handoff_seller_reply_and_fulfillment_survive_restart(tmp_path):
    settings = Settings(str(tmp_path / "orders.sqlite3"), ORIGIN, setup_key=SETUP, default_owner_code=OWNER)
    app = create_app(settings)
    with TestClient(app, base_url=ORIGIN) as buyer, TestClient(app, base_url=ORIGIN) as seller:
        headers, order = storefront(buyer)
        start = message(buyer, headers, order, START).json()["order"]
        case = message(buyer, headers, start, "Нужна индивидуальная смета.").json()
        assert case["outcome"] == "handoff"
        pending = case["order"]
        assert pending["seller_review"] == {"status": "pending"}
        validate("Order", pending)
        assert confirm(buyer, key(headers), pending).json()["code"] == "seller_review_required"
        assert message(buyer, headers, pending, CHANGE).json()["code"] == "seller_review_required"
        path = f"/merchant/orders/{order['id']}"
        outsider = register(seller, "other")
        assert seller.get(path).status_code == 404
        assert seller.post(
            path + "/reply", data={"token": outsider, "reply": "abc", "review_id": case["message_id"]}, headers={"Origin": ORIGIN},
        ).status_code == 404
        owner = register(seller, "coffee")
        assert "Нужен ответ" in seller.get("/merchant").text
        assert "Нужна индивидуальная смета." in seller.get(path).text
        assert seller.post(path + "/fulfill", data={"token": owner}, headers={"Origin": ORIGIN}).status_code == 409
        assert seller.post(
            path + "/reply", data={"token": owner, "reply": "Можем заменить позиции по прайсу.", "review_id": "old-review"},
            headers={"Origin": ORIGIN},
        ).status_code == 409
        assert seller.post(
            path + "/reply", data={"token": owner, "reply": "Можем заменить позиции по прайсу.", "review_id": case["message_id"]},
            headers={"Origin": ORIGIN},
        ).status_code == 200
        url = f"/api/v1/orders/{order['id']}"
        answered = buyer.get(url).json()
        assert answered["version"] > pending["version"]
        assert answered["seller_review"] == {"status": "answered", "reply": "Можем заменить позиции по прайсу."}
        validate("Order", answered)
        assert confirm(buyer, key(headers), answered).json()["code"] == "seller_review_required"
        revised = message(buyer, headers, answered, CHANGE).json()["order"]
        assert "seller_review" not in revised and revised["total_kopecks"] == 735_000
        assert "Можем заменить позиции по прайсу." in seller.get(path).text
        attempt = confirm(buyer, key(headers), revised)
        location = attempt.headers["location"]
        simulated = buyer.post("/demo" + location.removeprefix("/api/v1") + "/approve", headers=headers, json={})
        assert simulated.json()["status"] == "succeeded"
        for _ in range(2):
            fulfilled = seller.post(path + "/fulfill", data={"token": owner}, headers={"Origin": ORIGIN})
            assert fulfilled.status_code == 200 and "Исполнен" in fulfilled.text
        cookies = dict(buyer.cookies)

    with TestClient(create_app(settings), base_url=ORIGIN, cookies=cookies) as buyer:
        reloaded = buyer.get(url).json()
        assert reloaded["fulfilled"] is True and reloaded["status"] == "paid"
        assert "Можем заменить позиции по прайсу." in buyer.get("/").text


def test_upgrade_restores_block_for_latest_legacy_handoff(tmp_path):
    settings = Settings(str(tmp_path / "legacy.sqlite3"), ORIGIN)
    with TestClient(create_app(settings), base_url=ORIGIN) as buyer:
        headers, order = storefront(buyer)
        ready = message(buyer, headers, order, START).json()["order"]
        cookies = dict(buyer.cookies)
    with Store(settings.db_path, "simulated").transaction() as db:
        db.execute("DELETE FROM settings WHERE key='migration:seller-reviews'")
        db.execute(
            """INSERT INTO messages(id,order_id,request_key,base_version,text,lease,started_at,result)
               VALUES ('legacy-review', ?, 'legacy-key', ?, 'question', 'legacy-lease', 1, ?)""",
            (order["id"], ready["version"], json.dumps({
                "message_id": "legacy-review", "outcome": "handoff", "reply": "Seller clarification", "order": ready,
            })),
        )
    for _ in range(2):
        with TestClient(create_app(settings), base_url=ORIGIN, cookies=cookies) as buyer:
            current = buyer.get(f"/api/v1/orders/{order['id']}").json()
            assert current["version"] == ready["version"] + 1
            assert current["seller_review"] == {"status": "pending"}
            assert confirm(buyer, key(headers), current).json()["code"] == "seller_review_required"


def test_confirm_waits_for_other_tab_to_process_handoff(tmp_path):
    started, finish = Event(), Event()

    class GatedAgent(DemoAgent):
        def propose(self, catalog, quantities, recent, text):
            if text == "Нужна индивидуальная смета.":
                started.set()
                assert finish.wait(5)
            return super().propose(catalog, quantities, recent, text)

    settings = Settings(str(tmp_path / "parallel.sqlite3"), ORIGIN)
    with TestClient(create_app(settings, agent=GatedAgent()), base_url=ORIGIN) as buyer:
        headers, order = storefront(buyer)
        ready = message(buyer, headers, order, START).json()["order"]
        with ThreadPoolExecutor() as pool:
            future = pool.submit(message, buyer, headers, ready, "Нужна индивидуальная смета.")
            try:
                assert started.wait(5)
                blocked = confirm(buyer, key(headers), ready)
                assert blocked.status_code == 409
                assert blocked.json()["code"] == "request_in_progress"
            finally:
                finish.set()
            pending = future.result(timeout=5).json()["order"]
        assert confirm(buyer, key(headers), pending).json()["code"] == "seller_review_required"


def test_production_model_adapter_rejects_amount_and_keeps_order_unchanged(tmp_path, monkeypatch):
    returned = iter([
        {"outcome": "order_updated", "quantities": {"breakfast": 15, "vegetarian": 5}, "reply": "Состав готов."},
        {"outcome": "order_updated", "quantities": {"breakfast": 13, "vegetarian": 7}, "reply": "Изменил.", "amount": 1},
        {"outcome": "order_updated", "quantities": {"breakfast": 13, "vegetarian": 7}, "reply": "Изменил."},
    ])
    requests = []

    def model_post(url, *, headers, **options):
        assert url == "https://provider.example/v1/chat/completions"
        assert headers["Authorization"] == "Bearer fixture-key"
        requests.append(options["json"])
        return httpx.Response(
            200, json={"choices": [{"message": {"content": json.dumps(next(returned))}}]},
            request=httpx.Request("POST", url),
        )

    monkeypatch.setattr("app.agent.httpx.post", model_post)
    settings = Settings(
        str(tmp_path / "adapter.sqlite3"), ORIGIN, mode="live", llm_key="fixture-key", llm_url="https://provider.example/v1",
    )
    with TestClient(create_app(settings), base_url=ORIGIN) as client:
        headers, order = storefront(client)
        first = message(client, headers, order, START).json()["order"]
        assert first["total_kopecks"] == 725_000
        key_for_retry = str(uuid4())
        invalid = message(client, headers, first, CHANGE, key_for_retry)
        assert invalid.status_code == 503
        assert client.get(f"/api/v1/orders/{order['id']}").json() == first
        fixed = message(client, headers, first, CHANGE, key_for_retry).json()["order"]
        assert fixed["total_kopecks"] == 735_000
        prompt = json.loads(requests[-1]["messages"][-1]["content"])
        assert set(prompt["catalog"]) == {"breakfast", "vegetarian"}
        assert "fixture-key" not in str(requests)
