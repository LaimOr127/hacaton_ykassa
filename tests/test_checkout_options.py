import json
import smtplib
from uuid import uuid4

from fastapi.testclient import TestClient

from app.agent import DemoAgent
from app.gateway import Payment, ProviderAmount, amount_value
from app.main import create_app
from app.merchants import CLEANING_CSV
from app.settings import Settings
from app.storage import Store
from test_merchants import ORIGIN, SETUP, cleaning_order, publish, register


def options(client, token, *enabled):
    return client.post("/merchant/checkout-settings", headers={"Origin": ORIGIN},
                       data={"token": token, **dict.fromkeys(enabled, "true")})


def attempt(client, path, headers, body, key=None):
    return client.post(path + "/payment-attempts", json=body, headers={**headers, "Idempotency-Key": key or str(uuid4())})


def test_split_preserves_amounts_through_restart_and_blocks_early_fulfillment(tmp_path):
    settings = Settings(str(tmp_path / "split.sqlite3"), ORIGIN, setup_key=SETUP)
    with TestClient(create_app(settings), base_url=ORIGIN) as client:
        token = register(client)
        assert publish(client, token).status_code == 200
        headers, order, path = cleaning_order(client)
        first_body = {"order_version": order["version"], "parts": 4, "tip_percent": 5, "part_number": 1}
        assert attempt(client, path, headers, first_body).json()["code"] == "checkout_option_unavailable"
        assert options(client, token, "split_payments", "tips", "receipt_download").status_code == 200
        key = str(uuid4())
        first = attempt(client, path, headers, first_body, key)
        assert first.status_code == 201 and first.json()["amount_kopecks"] == 41_738
        assert attempt(client, path, headers, first_body, key).status_code == 200
        assert attempt(client, path, headers, {**first_body, "tip_percent": 10}, key).json()["code"] == "idempotency_conflict"
        assert attempt(client, path, headers, first_body).json()["code"] == "payment_in_progress"
        approve = "/demo" + first.headers["Location"].removeprefix("/api/v1") + "/approve"
        assert client.post(approve, json={}, headers=headers).json()["status"] == "succeeded"
        summary = client.get(path, headers=headers).json()
        assert summary["status"] == "payment_pending"
        assert summary["checkout"] == {
            "parts": 4, "paid_parts": 1, "tip_percent": 5, "tip_kopecks": 7_950,
            "total_kopecks": 166_950, "paid_kopecks": 41_738, "remaining_kopecks": 125_212, "next_kopecks": 41_738,
        }
        assert attempt(client, path, headers, first_body).json()["code"] == "checkout_changed"
        seller_url = f"/merchant/orders/{order['id']}/fulfill"
        assert client.post(seller_url, data={"token": token}, headers={"Origin": ORIGIN}).status_code == 409
        document = path + "/documents/" + first.json()["id"] + "/confirmation"
        assert client.get(document, headers=headers).status_code == 200
        assert "Симуляция" in client.get(document, headers=headers).text
        assert publish(client, token, CLEANING_CSV.replace("450000", "550000")).status_code == 200
        assert options(client, token, "receipt_download").status_code == 200
        cookies = dict(client.cookies)

    with TestClient(create_app(settings), base_url=ORIGIN, cookies=cookies) as client:
        for part, expected in [(2, 41_738), (3, 41_737), (4, 41_737)]:
            payment = attempt(client, path, headers, {**first_body, "part_number": part})
            assert payment.status_code == 201 and payment.json()["amount_kopecks"] == expected
            approve = "/demo" + payment.headers["Location"].removeprefix("/api/v1") + "/approve"
            assert client.post(approve, json={}, headers=headers).json()["status"] == "succeeded"
        summary = client.get(path, headers=headers).json()
        assert summary["status"] == "deposit_received" and summary["checkout"]["paid_kopecks"] == 166_950
        assert summary["balance_kopecks"] == 371_000
        assert client.post(seller_url, data={"token": token}, headers={"Origin": ORIGIN}).status_code == 200
        assert len(client.get(path + "/documents", headers=headers).json()["payments"]) == 4


class Gateway:
    def __init__(self):
        self.payments = {}
        self.status = "succeeded"

    def create(self, amount, attempt_id):
        payment = Payment(
            id=attempt_id, status="pending", amount=ProviderAmount(value=amount_value(amount), currency="RUB"),
            description=f"Order {attempt_id}", confirmation_url="https://yookassa.ru/checkout/fixture",
        )
        self.payments[attempt_id] = payment
        return payment

    def get(self, external_id):
        return self.payments[external_id].model_copy(update={"status": self.status})


def test_cancelled_later_part_retries_same_amount(tmp_path):
    settings = Settings(str(tmp_path / "retry.sqlite3"), ORIGIN, mode="live", setup_key=SETUP)
    gateway = Gateway()
    with TestClient(create_app(settings, agent=DemoAgent(), gateway=gateway), base_url=ORIGIN) as client:
        token = register(client)
        assert publish(client, token).status_code == 200
        assert options(client, token, "split_payments").status_code == 200
        headers, order, path = cleaning_order(client)
        body = {"order_version": order["version"], "parts": 2}
        first = attempt(client, path, headers, body)
        assert client.get(first.headers["Location"], headers=headers).json()["status"] == "succeeded"
        gateway.status = "canceled"
        second = attempt(client, path, headers, {**body, "part_number": 2})
        assert client.get(second.headers["Location"], headers=headers).json()["status"] == "canceled"
        stalled = client.get(path, headers=headers).json()
        assert stalled["status"] == "payment_pending" and stalled["checkout"]["paid_parts"] == 1
        retry = attempt(client, path, headers, {**body, "part_number": 2})
        assert retry.json()["part_number"] == 2 and retry.json()["amount_kopecks"] == second.json()["amount_kopecks"]


def test_receipt_download_email_idempotency_and_order_ownership(tmp_path, monkeypatch):
    settings = Settings(str(tmp_path / "docs.sqlite3"), ORIGIN, mode="live", setup_key=SETUP,
                        mail_host="mail.example.test", mail_from="store@example.test")
    delivered = []

    class SMTP:
        def __init__(self, host, port, *, timeout, context):
            assert host == settings.mail_host and port == 465 and timeout > 0 and context.check_hostname

        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

        def send_message(self, message):
            delivered.append(message)
            if message["To"] == "timeout@example.test":
                raise smtplib.SMTPServerDisconnected()
            if message["To"] == "reject@example.test":
                raise smtplib.SMTPRecipientsRefused({"reject@example.test": (550, b"test refusal")})

    monkeypatch.setattr("app.payment_documents.smtplib.SMTP_SSL", SMTP)
    gateway = Gateway()
    app = create_app(settings, agent=DemoAgent(), gateway=gateway)
    with TestClient(app, base_url=ORIGIN) as client, TestClient(app, base_url=ORIGIN) as stranger:
        token = register(client)
        assert publish(client, token).status_code == 200
        assert options(client, token, "receipt_download").status_code == 200
        headers, order, path = cleaning_order(client)
        response = attempt(client, path, headers, {"order_version": order["version"]})
        paid = response.json()
        docs = path + "/documents"
        confirmation = docs + "/" + paid["id"] + "/confirmation"
        receipt = docs + "/" + paid["id"] + "/receipt"
        assert client.get(confirmation, headers=headers).status_code == 404
        assert client.get(response.headers["Location"], headers=headers).json()["status"] == "succeeded"
        assert "1590.00 ₽" in client.get(confirmation, headers=headers).text
        upload = f"/merchant/orders/{order['id']}/receipts/{paid['id']}"
        pdf = b"%PDF-1.7\n" + b"a" * 30_000 + b"\n%%EOF"
        body = {"token": token}
        assert client.post(upload, data=body, headers={"Origin": ORIGIN},
                           files={"file": ("document.pdf", pdf, "application/pdf")}).status_code == 200
        assert client.post(upload, data=body, headers={"Origin": ORIGIN},
                           files={"file": ("document.pdf", b"invalid", "application/pdf")}).status_code == 400
        alias = f"/s/cleaning/orders/{order['id']}/documents/{paid['id']}/receipt"
        assert client.get(alias).content == pdf
        stranger.get("/s/cleaning")
        assert stranger.get(alias).status_code == 404
        assert client.get(docs, headers=headers).json()["payments"][0]["has_receipt"]

        method = receipt + "/email"
        key = str(uuid4())
        with_key = {**headers, "Idempotency-Key": key}
        mail = {"recipient": "buyer@example.test"}
        assert client.post(method, json=mail, headers=with_key).status_code == 404
        assert options(client, token, "receipt_email").status_code == 200
        assert client.get(alias).status_code == 404
        assert client.post(method, json=mail, headers={**with_key, "X-CSRF-Token": "wrong"}).status_code == 403
        assert client.post(method, json={"recipient": "a@example.test\nBCC:b@example.test"}, headers=with_key).status_code == 400
        assert client.post(method, json=mail, headers=with_key).json()["status"] == "accepted"
        assert client.post(method, json=mail, headers=with_key).json()["status"] == "accepted"
        assert len(delivered) == 1
        assert next(delivered[0].iter_attachments()).get_payload(decode=True) == pdf
        new_key = {**headers, "Idempotency-Key": str(uuid4())}
        assert client.post(method, json=mail, headers=new_key).json()["status"] == "accepted"
        assert client.post(method, json={"recipient": "other@example.test"}, headers=new_key).status_code == 429
        assert client.post(method, json={"recipient": "other@example.test"}, headers=with_key).status_code == 409
        assert "buyer@example.test" not in json.dumps(client.get(docs, headers=headers).json())
        with Store(settings.db_path, "live").transaction() as db:
            db.execute("UPDATE receipt_deliveries SET created_at = created_at - 61")
        timeout_key = {**headers, "Idempotency-Key": str(uuid4())}
        for _ in range(2):
            assert client.post(method, json={"recipient": "timeout@example.test"}, headers=timeout_key).json()["status"] == "unknown"
        with Store(settings.db_path, "live").transaction() as db:
            db.execute("UPDATE receipt_deliveries SET created_at = created_at - 61")
        refreshed = client.post(method, json={"recipient": "timeout@example.test"},
                                headers={**headers, "Idempotency-Key": str(uuid4())})
        assert refreshed.json()["status"] == "unknown"
        assert len(delivered) == 2
        rejected = client.post(method, json={"recipient": "reject@example.test"},
                               headers={**headers, "Idempotency-Key": str(uuid4())})
        assert rejected.json()["status"] == "failed"
        with Store(settings.db_path, "live").transaction() as db:
            db.execute("UPDATE receipt_deliveries SET created_at = created_at - 61")
        retry = client.post(method, json={"recipient": "reject@example.test"},
                            headers={**headers, "Idempotency-Key": str(uuid4())})
        assert retry.json()["status"] == "failed"
        assert len(delivered) == 4
