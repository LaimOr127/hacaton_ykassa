import json
from contextlib import asynccontextmanager

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
