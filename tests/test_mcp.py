import json
from contextlib import asynccontextmanager
from types import SimpleNamespace

import httpx
import pytest

from app import gateway


def test_mcp_sdk_calls_official_payment_tools(monkeypatch):
    calls = []

    def fake_mcp(request):
        assert str(request.url) == "https://mcp.yookassa.ru"
        assert request.headers["authorization"] == "Bearer test-token-placeholder"
        assert request.method == "POST"
        rpc = json.loads(request.content)
        method = rpc["method"]
        if method == "notifications/initialized":
            return httpx.Response(202)
        if method == "initialize":
            result = {
                "protocolVersion": rpc["params"]["protocolVersion"],
                "capabilities": {},
                "serverInfo": {"name": "fixture", "version": "1"},
            }
        elif method == "tools/list":
            result = {
                "tools": [
                    {"name": name, "inputSchema": {"type": "object"}}
                    for name in ("create_payment", "get_payment")
                ]
            }
        else:
            params = rpc["params"]
            assert method == "tools/call"
            calls.append((params["name"], params["arguments"]))
            payment = {
                "id": "payment-123",
                "status": "pending" if params["name"] == "create_payment" else "succeeded",
                "amount": {"value": "7350.00", "currency": "RUB"},
            }
            if payment["status"] == "pending":
                payment["confirmation_url"] = "https://yookassa.ru/checkout/test"
                result = {"content": [{"type": "text", "text": json.dumps(payment)}], "isError": False}
            else:
                result = {"content": [], "structuredContent": payment, "isError": False}
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": rpc["id"], "result": result})

    actual_transport = gateway.streamable_http_client

    @asynccontextmanager
    async def local_transport(url, *, http_client):
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(fake_mcp), headers=http_client.headers,
        ) as client:
            async with actual_transport(url, http_client=client) as streams:
                yield streams

    monkeypatch.setattr(gateway, "streamable_http_client", local_transport)
    provider = gateway.YooKassa("test-token-placeholder")
    assert provider.check() is True
    payment = provider.create(735_000, "attempt-123")
    assert payment.status == "pending"
    assert payment.kopecks == 735_000
    assert gateway.trusted_checkout(payment.confirmation_url) == payment.confirmation_url
    assert provider.get(payment.id).status == "succeeded"
    assert calls == [
        ("create_payment", {"amount": {"value": "7350.00", "currency": "RUB"}, "description": "Order attempt-123"}),
        ("get_payment", {"id": "payment-123"}),
    ]


@pytest.mark.parametrize("failure", ["handshake", "connect", "read", "invalid_get"])
def test_transport_distinguishes_unsubmitted_create_and_invalid_read(monkeypatch, failure):
    actual_transport = gateway.streamable_http_client

    def fake_mcp(request):
        rpc = json.loads(request.content)
        if failure == "handshake":
            assert rpc["method"] == "initialize"
            return httpx.Response(401)
        if rpc["method"] == "initialize":
            return httpx.Response(200, json={
                "jsonrpc": "2.0",
                "id": rpc["id"],
                "result": {
                    "protocolVersion": rpc["params"]["protocolVersion"],
                    "capabilities": {},
                    "serverInfo": {"name": "fixture", "version": "1"},
                },
            })
        if rpc["method"] == "notifications/initialized":
            return httpx.Response(202)
        assert rpc["method"] == "tools/call"
        if failure == "connect":
            raise httpx.ConnectError("Соединение не установлено")
        if failure == "invalid_get":
            return httpx.Response(200, json={
                "jsonrpc": "2.0", "id": rpc["id"],
                "result": {"content": [], "structuredContent": []},
            })
        raise httpx.ReadError("Соединение оборвалось")

    @asynccontextmanager
    async def local_transport(url, *, http_client):
        async with httpx.AsyncClient(transport=httpx.MockTransport(fake_mcp)) as client:
            async with actual_transport(url, http_client=client) as streams:
                yield streams

    monkeypatch.setattr(gateway, "streamable_http_client", local_transport)
    provider = gateway.YooKassa("test-token-placeholder")
    if failure == "invalid_get":
        with pytest.raises(gateway.InvalidPaymentResponse):
            provider.get("attempt-123")
    else:
        expected = Exception if failure == "read" else gateway.PaymentNotSent
        with pytest.raises(expected) as recorded:
            provider.create(1000, "attempt-123")
    if failure == "read":
        assert not isinstance(recorded.value, gateway.PaymentNotSent)


def test_invoice_cart_and_read_only_mcp_tools(monkeypatch):
    calls = []
    amount = {"value": "1590.00", "currency": "RUB"}
    payment = {"id": "payment-found", "status": "succeeded", "amount": amount, "description": "Order attempt-1"}
    refund = {"id": "refund-found", "payment_id": "payment-found", "status": "succeeded", "amount": amount}
    invoice = {
        "id": "invoice-1", "status": "pending", "amount": amount, "description": "Order attempt-1",
        "url": "https://yookassa.ru/invoice/demo", "expires_at": gateway.utc_iso(1000 + gateway.INVOICE_LIFETIME),
    }

    class ToolSession:
        async def list_tools(self):
            return SimpleNamespace(tools=[SimpleNamespace(name=name) for name in gateway.YOO_TOOLS])

        async def call_tool(self, name, arguments):
            calls.append((name, arguments))
            if name in {"create_invoice", "get_invoice"}:
                result = invoice
            elif name == "get_payments_list":
                result = {"items": [payment]} if "next_cursor" in arguments else {
                    "items": [{**payment, "id": "other", "status": "waiting_for_capture", "description": "Other"}],
                    "next_cursor": "page-2",
                }
            elif name == "get_refunds_list":
                result = {"items": [refund]}
            else:
                assert name == "get_refund"
                result = refund
            return SimpleNamespace(isError=False, structuredContent=result)

    @asynccontextmanager
    async def session(_):
        yield ToolSession()

    monkeypatch.setattr(gateway.YooKassa, "session", session)
    provider = gateway.YooKassa("fixture-test-token")
    assert provider.tools() == set(gateway.YOO_TOOLS)
    snapshot = {"payable_kopecks": 159_000, "balance_kopecks": 371_000}
    assert provider.create_invoice(snapshot, "attempt-1", "order-12345", 1000).confirmation_url == invoice["url"]
    assert calls[0] == ("create_invoice", {
        "amount": amount, "description": "Order attempt-1", "expires_at": invoice["expires_at"],
        "cart": [{"description": "Предоплата за заказ #order-12", "price": amount, "quantity": 1}],
    })
    assert provider.get_invoice("invoice-1").id == "invoice-1"
    assert provider.find_payment(1000, "attempt-1", 159_000) == "payment-found"
    assert calls[2][1]["created_at_gte"] == gateway.utc_iso(880)
    assert calls[3][1]["next_cursor"] == "page-2"
    assert provider.list_refunds("payment-found")[0].id == provider.get_refund("refund-found").id
    assert calls[4:] == [
        ("get_refunds_list", {"payment_id": "payment-found", "limit": 100}),
        ("get_refund", {"id": "refund-found"}),
    ]
