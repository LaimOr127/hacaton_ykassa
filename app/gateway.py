import json
import uuid
from contextlib import asynccontextmanager
from decimal import Decimal
from typing import Literal
from urllib.parse import urlsplit

import anyio
import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from pydantic import BaseModel, Field, StrictStr, ValidationError

from .storage import Store


class ProviderAmount(BaseModel):
    value: StrictStr = Field(pattern=r"^\d{1,14}\.\d{2}$")
    currency: StrictStr = Field(pattern=r"^[A-Z]{3}$")


class Payment(BaseModel):
    id: StrictStr = Field(min_length=1, max_length=128)
    status: Literal["pending", "succeeded", "canceled"]
    amount: ProviderAmount
    confirmation_url: StrictStr | None = None
    description: StrictStr | None = None

    @property
    def kopecks(self) -> int:
        return int(Decimal(self.amount.value) * 100)


class PaymentNotSent(Exception):
    """Ошибка до передачи вызова create_payment серверу."""


class InvalidPaymentResponse(Exception):
    def __init__(self, external_id=None):
        self.external_id = external_id
        super().__init__("Некорректный ответ MCP")


def only_errors(error: Exception, types: tuple[type[Exception], ...]) -> bool:
    if isinstance(error, ExceptionGroup):
        matched, rest = error.split(types)
        return matched is not None and rest is None
    return isinstance(error, types)


def has_error(error: Exception, types: tuple[type[Exception], ...]) -> bool:
    if isinstance(error, ExceptionGroup):
        return error.subgroup(types) is not None
    return isinstance(error, types)


def amount_value(kopecks: int) -> str:
    return f"{kopecks // 100}.{kopecks % 100:02d}"


def payment_reference(attempt_id: str) -> str:
    return f"Order {attempt_id}"


def trusted_checkout(url: str | None) -> str | None:
    if not url or len(url) > 2048 or any(c.isspace() or c == "\\" for c in url):
        return None
    parsed = urlsplit(url)
    host = parsed.hostname or ""
    if (
        parsed.scheme != "https"
        or parsed.username is not None
        or parsed.port not in {443, None}
        or not any(host == base or host.endswith("." + base) for base in ("yookassa.ru", "yoomoney.ru"))
    ):
        return None
    return url


class YooKassa:
    def __init__(self, token: str):
        self.token = token

    @asynccontextmanager
    async def session(self):
        with anyio.fail_after(18):
            async with httpx.AsyncClient(
                headers={"Authorization": f"Bearer {self.token}"}, timeout=10, follow_redirects=False,
            ) as client:
                async with streamable_http_client("https://mcp.yookassa.ru", http_client=client) as (read, write, _):
                    async with ClientSession(read, write) as session:
                        await session.initialize()
                        yield session

    def check(self) -> bool:
        async def available():
            async with self.session() as session:
                listed = await session.list_tools()
                return {"create_payment", "get_payment"} <= {tool.name for tool in listed.tools}

        return anyio.run(available)

    async def call(self, name: str, arguments: dict) -> Payment:
        tool_started = False
        try:
            async with self.session() as session:
                tool_started = True
                response = await session.call_tool(name, arguments)
        except Exception as error:
            connection_refused = only_errors(error, (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout))
            if name == "create_payment" and (not tool_started or connection_refused):
                raise PaymentNotSent() from error
            if name == "get_payment" and has_error(error, (ValidationError, ValueError)):
                # SDK валидирует JSON-RPC до возврата результата инструмента.
                raise InvalidPaymentResponse() from error
            raise
        if response.isError:
            raise ValueError("MCP вернул ошибку операции")
        data = response.structuredContent
        try:
            if data is None and len(response.content) == 1 and response.content[0].type == "text":
                data = json.loads(response.content[0].text)
            return Payment.model_validate(data)
        except (ValidationError, ValueError) as error:
            external_id = data.get("id") if isinstance(data, dict) else None
            if isinstance(external_id, str) and 1 <= len(external_id) <= 128:
                raise InvalidPaymentResponse(external_id) from error
            raise InvalidPaymentResponse() from error

    def create(self, amount: int, attempt_id: str) -> Payment:
        return anyio.run(
            self.call,
            "create_payment",
            {"amount": {"value": amount_value(amount), "currency": "RUB"}, "description": payment_reference(attempt_id)},
        )

    def get(self, external_id: str) -> Payment:
        return anyio.run(self.call, "get_payment", {"id": external_id})


class Simulator:
    def __init__(self, store: Store):
        self.store = store

    def create(self, amount: int, attempt_id: str) -> Payment:
        external_id = uuid.uuid4().hex
        with self.store.transaction() as db:
            db.execute("INSERT INTO demo_payments(id, amount_kopecks) VALUES (?, ?)", (external_id, amount))
        return self.get(external_id)

    def get(self, external_id: str) -> Payment:
        with self.store.read() as db:
            row = db.execute("SELECT * FROM demo_payments WHERE id = ?", (external_id,)).fetchone()
        return Payment(
            id=external_id,
            status=row["state"],
            amount=ProviderAmount(value=amount_value(row["amount_kopecks"]), currency="RUB"),
        )

    def approve(self, external_id: str):
        with self.store.transaction() as db:
            db.execute("UPDATE demo_payments SET state = 'succeeded' WHERE id = ?", (external_id,))
