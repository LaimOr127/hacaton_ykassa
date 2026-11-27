import re
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from app.agent import DemoAgent
from app.gateway import InvalidPaymentResponse, Payment, ProviderAmount
from app.main import create_app
from app.settings import Settings

ORIGIN = "https://example.test"
SETUP = "fixture-setup-code-2026-only"
OWNER = "fixture-owner-code"


class LostReply:
    reference = ""
    reads = 0

    def create(self, amount, attempt_id):
        self.reference = f"Order {attempt_id}"
        raise TimeoutError

    def get(self, external_id):
        self.reads += 1
        return Payment(
            id=external_id,
            amount=ProviderAmount(value="7249.00" if external_id == "wrong-amount" else "7250.00", currency="RUB"),
            status="canceled" if external_id == "canceled" else "succeeded",
            description=self.reference if external_id != "wrong-reference" else "Unrelated payment",
        )

    def find_payment(self, created_at, attempt_id, amount):
        assert created_at > 0 and self.reference == f"Order {attempt_id}" and amount == 725_000
        return "succeeded"


def manager(client, slug):
    landing = client.get("/merchant/login")
    token = re.search(r'name="token" value="([^"]+)"', landing.text).group(1)
    response = client.post("/merchant/register", data={
        "token": token, "setup": SETUP, "slug": slug, "name": slug, "code": OWNER,
    }, headers={"Origin": ORIGIN})
    assert response.status_code == 200
    return re.search(r'name="token" value="([^"]+)"', response.text).group(1)


@pytest.mark.parametrize("final_status", ["succeeded", "canceled"])
def test_operator_recovers_only_verified_payment(tmp_path, final_status):
    settings = Settings(
        str(tmp_path / "recovery.sqlite3"), ORIGIN, mode="live", llm_key="fixture", setup_key=SETUP,
        default_owner_code=OWNER,
    )
    provider = LostReply()
    with TestClient(create_app(settings, agent=DemoAgent(), gateway=provider), base_url=ORIGIN) as client:
        page = client.get("/")
        csrf = re.search(r'<meta name="csrf-token" content="([^"]+)"', page.text).group(1)
        headers = {"Origin": ORIGIN, "X-CSRF-Token": csrf}
        order = client.post("/api/v1/orders", headers={**headers, "Idempotency-Key": str(uuid4())}).json()
        url = f"/api/v1/orders/{order['id']}"
        order = client.post(url + "/messages", headers=headers, json={
            "order_version": order["version"], "client_message_id": str(uuid4()),
            "text": "20 завтраков, из них 5 вегетарианских",
        }).json()["order"]
        attempt = client.post(url + "/payment-attempts", headers={**headers, "Idempotency-Key": str(uuid4())},
                              json={"order_version": order["version"]}).json()
        assert attempt["status"] == "unknown"
        cookies = dict(client.cookies)

    with TestClient(create_app(settings, agent=DemoAgent(), gateway=provider), base_url=ORIGIN, cookies=cookies) as client:
        review = f"/merchant/payment-review/{attempt['id']}"
        check_url = url + f"/payment-attempts/{attempt['id']}"
        assert client.get(check_url).json()["status"] == "unknown"
        stranger = manager(client, "different")
        assert provider.reference not in client.get("/merchant").text
        wrong_owner = client.post(review, data={"token": stranger, "payment_id": final_status}, headers={"Origin": ORIGIN})
        assert wrong_owner.status_code == 404
        assert provider.reads == 0
        owner = manager(client, "coffee")
        assert provider.reference in client.get("/merchant").text
        assert "Оплата подтверждена." not in client.get("/merchant?updated=succeeded").text
        for payment_id in ("wrong-reference", "wrong-amount"):
            rejected = client.post(review, data={"token": owner, "payment_id": payment_id}, headers={"Origin": ORIGIN})
            assert rejected.status_code == 409
        assert client.get(check_url).json()["status"] == "unknown"
        selected = {"payment_id": "canceled"} if final_status == "canceled" else {}
        result = client.post(review, data={"token": owner, **selected}, headers={"Origin": ORIGIN})
        assert result.status_code == 200
        assert client.get(check_url).json()["status"] == final_status
        expected_order_state = "paid" if final_status == "succeeded" else "agreed"
        assert client.get(url).json()["status"] == expected_order_state
        reads = provider.reads
        repeated = client.post(review, data={"token": owner, "payment_id": final_status}, headers={"Origin": ORIGIN})
        assert repeated.status_code == 409
        assert provider.reads == reads


def test_candidate_from_invalid_create_needs_its_own_reference(tmp_path):
    class Candidate(LostReply):
        def create(self, amount, attempt_id):
            raise InvalidPaymentResponse("wrong-reference")

    settings = Settings(
        str(tmp_path / "candidate.sqlite3"), ORIGIN, mode="live", llm_key="fixture", setup_key=SETUP,
        default_owner_code=OWNER,
    )
    gateway = Candidate()
    with TestClient(create_app(settings, agent=DemoAgent(), gateway=gateway), base_url=ORIGIN) as client:
        page = client.get("/")
        csrf = re.search(r'<meta name="csrf-token" content="([^"]+)"', page.text).group(1)
        headers = {"Origin": ORIGIN, "X-CSRF-Token": csrf}
        order = client.post("/api/v1/orders", headers={**headers, "Idempotency-Key": str(uuid4())}).json()
        order_url = f"/api/v1/orders/{order['id']}"
        parsed = client.post(order_url + "/messages", headers=headers, json={
            "order_version": 1, "client_message_id": str(uuid4()), "text": "20 завтраков, из них 5 вегетарианских",
        }).json()["order"]
        attempt = client.post(order_url + "/payment-attempts", headers={**headers, "Idempotency-Key": str(uuid4())},
                              json={"order_version": parsed["version"]})
        assert client.get(attempt.headers["location"]).json()["status"] == "unknown"
        token = manager(client, "coffee")
        rejected = client.post(
            f"/merchant/payment-review/{attempt.json()['id']}", data={"token": token}, headers={"Origin": ORIGIN},
        )
        assert rejected.status_code == 409
        assert client.get(order_url).json()["status"] == "payment_pending"
