import base64
import hashlib
import time
from urllib.parse import parse_qs, urlsplit
from uuid import uuid4

import httpx
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from app.agent import DemoAgent, Proposal
from app.catalog import COFFEE
from app.gateway import INVOICE_LIFETIME, InvalidPaymentResponse, Invoice, Payment, ProviderAmount, Refund, utc_iso
from app.main import create_app
from app.merchants import Merchants
from app.settings import Settings
from app.storage import Store
from test_merchants import ORIGIN, OWNER, SETUP, cleaning_order, form_token, publish, register


def test_openrouter_authorizes_one_shop_once_and_selects_without_key(tmp_path, monkeypatch):
    settings = Settings(
        str(tmp_path / "oauth.sqlite3"), ORIGIN, mode="live", openrouter_oauth=True,
        setup_key=SETUP, default_owner_code=OWNER, vault_key=Fernet.generate_key().decode(),
    )
    app = create_app(settings)
    exchange = []
    authorized_key = "fixture-authorization-key"

    def post(url, *, json, timeout, follow_redirects):
        assert url == "https://openrouter.ai/api/v1/auth/keys" and not follow_redirects
        exchange.append(json)
        return httpx.Response(200, json={"key": authorized_key}, request=httpx.Request("POST", url))

    def check(connection, *_):
        assert connection.key == authorized_key
        assert connection.url == "https://openrouter.ai/api/v1"
        return Proposal(outcome="clarification", quantities={}, reply="Подключено")

    monkeypatch.setattr("app.model_oauth.httpx.post", post)
    monkeypatch.setattr("app.agent.ModelAgent.request", staticmethod(check))
    with TestClient(app, base_url=ORIGIN) as owner, TestClient(app, base_url=ORIGIN) as stranger:
        csrf = register(owner, "coffee")
        register(stranger, "other")
        headers = {"Origin": ORIGIN}
        start_url = "/merchant/model/openrouter"
        assert owner.post(start_url, data={"token": csrf}, headers={"Origin": "https://foreign.test"}).status_code == 403
        start = owner.post(start_url, data={"token": csrf}, headers=headers, follow_redirects=False)
        assert start.status_code == 303 and "samesite=lax" in start.headers["Set-Cookie"].lower()
        location = urlsplit(start.headers["Location"])
        params = parse_qs(location.query)
        assert location.netloc == "openrouter.ai" and params["code_challenge_method"] == ["S256"]
        callback = params["callback_url"][0]
        query = {"state": params["state"][0], "code": "fixture-code"}
        assert "model-account-error" in stranger.get(callback, params=query, follow_redirects=False).headers["Location"]
        assert not exchange
        login = stranger.get("/merchant/login")
        assert stranger.post("/merchant/login", data={"token": form_token(login.text), "slug": "coffee", "code": OWNER},
                             headers=headers).status_code == 200
        assert "model-account-error" in stranger.get(callback, params=query, follow_redirects=False).headers["Location"]
        assert not exchange
        result = owner.get(callback, params=query, follow_redirects=False)
        assert "updated=model-account#" in result.headers["Location"]
        assert result.headers["Referrer-Policy"] == "no-referrer"
        challenge = base64.urlsafe_b64encode(hashlib.sha256(exchange[0]["code_verifier"].encode()).digest()).decode().rstrip("=")
        assert params["code_challenge"] == [challenge]
        assert "model-account-error" in owner.get(callback, params=query, follow_redirects=False).headers["Location"]
        assert len(exchange) == 1
        dashboard = owner.get("/merchant")
        assert authorized_key not in dashboard.text
        assert owner.post(start_url + "/select", data={"token": csrf, "model": "other/model"}, headers=headers).status_code == 200
        merchants = Merchants(Store(settings.db_path, "live"), settings, COFFEE)
        assert merchants.model_connection("coffee").model == "other/model"
        assert merchants.model_connection("other") is None

        stale = owner.post(start_url, data={"token": csrf}, headers=headers, follow_redirects=False)
        stale_state = parse_qs(urlsplit(stale.headers["Location"]).query)["state"][0]
        assert owner.post("/merchant/model/disconnect", data={"token": csrf}, headers=headers).status_code == 200
        rejected = owner.get(callback, params={**query, "state": stale_state}, follow_redirects=False)
        assert "model-account-error" in rejected.headers["Location"] and len(exchange) == 1
        assert merchants.model_connection("coffee") is None


class InvoiceGateway:
    checkout = "https://yookassa.ru/invoice/fixture"
    amount = ProviderAmount(value="1590.00", currency="RUB")

    def __init__(self):
        self.created = 0
        self.direct = 0
        self.invoice = None
        self.linked_amount = ProviderAmount(value="1591.00", currency="RUB")
        self.refund_checks = []

    def create(self, *_):
        self.direct += 1
        raise AssertionError("Direct payment must not be created")

    def create_invoice(self, snapshot, attempt_id, order_id, created_at):
        self.created += 1
        assert snapshot["total_kopecks"] == 530_000 and snapshot["payable_kopecks"] == 159_000
        self.invoice = Invoice.model_validate({
            "id": "invoice-fixture", "status": "pending", "amount": self.amount,
            "description": f"Order {attempt_id}", "url": self.checkout,
            "expires_at": utc_iso(created_at + INVOICE_LIFETIME),
        })
        return self.invoice

    def get_invoice(self, external_id):
        assert external_id == self.invoice.id
        return self.invoice

    def get(self, external_id):
        return Payment(id=external_id, status="succeeded", amount=self.linked_amount, description="Другой формат описания")

    def list_refunds(self, payment_id):
        assert payment_id == "linked-payment"
        return [Refund(id="refund-for-order", payment_id=payment_id, status="succeeded", amount=self.amount),
                Refund(id="unrelated-refund", payment_id="different-payment", status="succeeded", amount=self.amount)]

    def get_refund(self, refund_id):
        self.refund_checks.append(refund_id)
        return self.list_refunds("linked-payment")[0]


def test_invoice_and_payment_share_reservation_and_require_linked_confirmation(tmp_path):
    settings = Settings(str(tmp_path / "invoice.sqlite3"), ORIGIN, mode="live", mcp_invoices=True, setup_key=SETUP)
    provider = InvoiceGateway()
    app = create_app(settings, agent=DemoAgent(), gateway=provider)
    with TestClient(app, base_url=ORIGIN) as client:
        token = register(client)
        assert publish(client, token).status_code == 200
        headers, order, path = cleaning_order(client)
        request = {"order_version": order["version"], "kind": "invoice"}
        with_key = {**headers, "Idempotency-Key": str(uuid4())}
        endpoint = path + "/payment-attempts"
        answer = client.post(endpoint, json=request, headers=with_key)
        assert answer.status_code == 201
        url = answer.headers["Location"]
        attempt = answer.json()
        assert attempt["kind"] == "invoice" and attempt["confirmation_url"] == provider.checkout
        assert client.post(endpoint, json=request, headers=with_key).json() == attempt
        direct = {**request, "kind": "payment"}
        assert client.post(endpoint, json=direct, headers=with_key).json()["code"] == "idempotency_conflict"
        assert client.post(endpoint, json=direct, headers={**headers, "Idempotency-Key": str(uuid4())}).json()["code"] == "payment_in_progress"
        assert provider.created == 1 and provider.direct == 0
        with Store(settings.db_path, "live").transaction() as db:
            db.execute("UPDATE attempts SET created_at = ? WHERE id = ?", (int(time.time()) - INVOICE_LIFETIME - 1, attempt["id"]))
        expired = client.post(endpoint, json=request, headers=with_key).json()
        assert expired["status"] == "pending" and "confirmation_url" not in expired
        assert client.post(endpoint, json=direct, headers={**headers, "Idempotency-Key": str(uuid4())}).json()["code"] == "payment_in_progress"

        provider.invoice = Invoice.model_validate({
            "id": provider.invoice.id, "status": "succeeded", "amount": provider.amount,
            "description": provider.invoice.description, "payment_details": {"id": "linked-payment", "status": "succeeded"},
        })
        mismatch = client.get(url, headers=headers).json()
        assert mismatch["status"] == "unknown" and "confirmation_url" not in mismatch
        assert client.get(path, headers=headers).json()["status"] == "payment_pending"
        seller_headers = {"Origin": ORIGIN}
        provider.linked_amount = provider.amount
        review = "/merchant/payment-review/" + attempt["id"]
        assert client.post(review, headers=seller_headers, data={"token": token}).status_code == 200
        assert client.get(path, headers=headers).json()["status"] == "deposit_received"

        refunds = "/merchant/orders/" + order["id"] + "/refunds"
        list_page = client.post(refunds, headers=seller_headers, data={"token": token})
        assert list_page.status_code == 200 and "refund-for-order" in list_page.text and "unrelated-refund" not in list_page.text
        assert client.post(refunds, headers=seller_headers, data={"token": token, "refund_id": "unrelated-refund"}).status_code == 404
        assert not provider.refund_checks
        assert client.post(refunds, headers=seller_headers, data={"token": token, "refund_id": "refund-for-order"}).status_code == 200
        assert provider.refund_checks == ["refund-for-order"]
        stranger = register(client, "other")
        assert client.post(refunds, headers=seller_headers, data={"token": stranger}).status_code == 404
        assert provider.refund_checks == ["refund-for-order"]


def test_manual_invoice_reconciliation_can_replace_unverified_candidate(tmp_path):
    class LostInvoice(InvoiceGateway):
        def create_invoice(self, snapshot, attempt_id, order_id, created_at):
            super().create_invoice(snapshot, attempt_id, order_id, created_at)
            raise InvalidPaymentResponse("wrong-candidate")

        def get_invoice(self, external_id):
            if external_id == "wrong-candidate":
                return self.invoice.model_copy(update={"id": external_id, "description": "Another order"})
            return super().get_invoice(external_id)

    settings = Settings(str(tmp_path / "candidate.sqlite3"), ORIGIN, mode="live", mcp_invoices=True, setup_key=SETUP)
    provider = LostInvoice()
    with TestClient(create_app(settings, agent=DemoAgent(), gateway=provider), base_url=ORIGIN) as client:
        token = register(client)
        assert publish(client, token).status_code == 200
        headers, order, path = cleaning_order(client)
        attempt = client.post(path + "/payment-attempts", json={"order_version": order["version"], "kind": "invoice"},
                              headers={**headers, "Idempotency-Key": str(uuid4())})
        assert attempt.json()["status"] == "unknown"
        assert client.get(attempt.headers["Location"], headers=headers).json()["status"] == "unknown"
        review = "/merchant/payment-review/" + attempt.json()["id"]
        result = client.post(review, headers={"Origin": ORIGIN}, data={"token": token, "payment_id": "invoice-fixture"})
        assert result.status_code == 200
        assert client.get(attempt.headers["Location"], headers=headers).json()["confirmation_url"] == provider.checkout


def test_old_payment_attempt_schema_is_preserved_on_migration(tmp_path):
    settings = Settings(str(tmp_path / "legacy.sqlite3"), ORIGIN, mode="live", setup_key=SETUP)
    store = Store(settings.db_path, "live")
    store.initialize()
    with store.transaction() as db:
        db.execute("DROP INDEX settled_invoice_payment")
        db.execute("ALTER TABLE attempts DROP COLUMN settled_payment_id")
        db.execute("ALTER TABLE attempts DROP COLUMN kind")
        db.execute("ALTER TABLE attempts DROP COLUMN candidate_id")
        db.execute("INSERT INTO sessions VALUES ('prior-session', 'coffee', 100)")
        db.execute("INSERT INTO orders(id,session_id,merchant,create_key,quote) VALUES ('prior-order','prior-session','coffee','key','{}')")
        db.execute(
            """INSERT INTO attempts(id,order_id,request_key,order_version,snapshot,state,created_at,external_id)
               VALUES ('prior-attempt','prior-order','key',2,'{}','succeeded',100,'provider-id')""",
        )
    Store(settings.db_path, "live").initialize()
    with store.read() as db:
        assert tuple(db.execute("SELECT external_id, kind, settled_payment_id FROM attempts").fetchone()) == ("provider-id", "payment", None)
