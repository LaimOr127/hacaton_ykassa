import json
import uuid
from contextlib import AsyncExitStack, asynccontextmanager
from contextvars import ContextVar
from datetime import UTC, datetime
from decimal import Decimal
from threading import BoundedSemaphore
from typing import Literal
from urllib.parse import urlsplit

import anyio
import httpx
from anyio.from_thread import BlockingPortal
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from pydantic import BaseModel, Field, StrictStr, ValidationError

from .storage import Store


class ProviderAmount(BaseModel):
    value: StrictStr = Field(pattern=r"^\d{1,14}\.\d{2}$")
    currency: StrictStr = Field(pattern=r"^[A-Z]{3}$")


class ListedPayment(BaseModel):
    id: StrictStr = Field(min_length=1, max_length=128)
    status: StrictStr
    amount: ProviderAmount
    confirmation_url: StrictStr | None = None
    description: StrictStr | None = None

    @property
    def kopecks(self) -> int:
        return int(Decimal(self.amount.value) * 100)


class Payment(ListedPayment):
    status: Literal["pending", "succeeded", "canceled"]


class LinkedPayment(BaseModel):
    id: StrictStr = Field(min_length=1, max_length=128)
    status: StrictStr


class Invoice(Payment):
    confirmation_url: StrictStr | None = Field(default=None, validation_alias="url")
    expires_at: datetime | None = None
    payment_details: LinkedPayment | None = None


class Refund(BaseModel):
    id: StrictStr = Field(min_length=1, max_length=128)
    payment_id: StrictStr = Field(min_length=1, max_length=128)
    status: Literal["pending", "succeeded", "canceled"]
    amount: ProviderAmount

    @property
    def kopecks(self):
        return int(Decimal(self.amount.value) * 100)


class MCPPage[Item: BaseModel](BaseModel):
    items: list[Item] = Field(max_length=100)
    next_cursor: StrictStr | None = Field(default=None, max_length=4096)


INVOICE_LIFETIME = 24 * 3600
YOO_TOOLS = {
    "create_payment": "Создание платежа", "get_payment": "Проверка платежа",
    "get_payments_list": "Поиск платежей", "get_refunds_list": "Список возвратов",
    "get_refund": "Детали возврата", "create_invoice": "Выставление счёта", "get_invoice": "Проверка счёта",
}


class PaymentNotSent(Exception):
    """Ошибка до передачи создания платежа или счёта серверу."""


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


def provider_amount(kopecks: int) -> dict:
    return {"value": amount_value(kopecks), "currency": "RUB"}


def utc_iso(timestamp: int) -> str:
    return datetime.fromtimestamp(timestamp, UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def payment_reference(attempt_id: str) -> str:
    return f"Order {attempt_id}"


def trusted_checkout(url: str | None) -> str | None:
    if not url or len(url) > 2048 or any(c.isspace() or c == "\\" for c in url):
        return None
    try:
        parsed = urlsplit(url)
        port = parsed.port
    except ValueError:
        return None
    host = parsed.hostname or ""
    if (
        parsed.scheme != "https"
        or parsed.username is not None
        or port not in {443, None}
        or not any(host == base or host.endswith("." + base) for base in ("yookassa.ru", "yoomoney.ru"))
    ):
        return None
    return url


class McpBusy(Exception):
    pass


class McpPool:
    def __init__(self):
        self.token = ContextVar("mcp_token")
        self.slots = BoundedSemaphore(6)

    async def authorize(self, request):
        request.headers.pop("Cookie", None)
        if request.url.scheme == "https" and request.url.host == "mcp.yookassa.ru" and request.url.port in (None, 443):
            request.headers["Authorization"] = "Bearer " + self.token.get()

    @asynccontextmanager
    async def open(self):
        async with AsyncExitStack() as stack:
            self.client = await stack.enter_async_context(httpx.AsyncClient(
                timeout=10, follow_redirects=False, trust_env=False, event_hooks={"request": [self.authorize]},
            ))
            self.portal = await stack.enter_async_context(BlockingPortal())
            yield

    def run(self, token, operation, *args):
        if not self.slots.acquire(blocking=False):
            raise McpBusy()
        try:
            async def authorized():
                context = self.token.set(token)
                try:
                    return await operation(*args)
                finally:
                    self.token.reset(context)

            return self.portal.call(authorized)
        finally:
            self.slots.release()


class YooKassa:
    pool = None
    def __init__(self, token: str):
        self.token = token

    @asynccontextmanager
    async def session(self):
        with anyio.fail_after(18):
            async with AsyncExitStack() as stack:
                client = self.pool.client if self.pool else await stack.enter_async_context(httpx.AsyncClient(
                    headers={"Authorization": f"Bearer {self.token}"}, timeout=10, follow_redirects=False,
                ))
                read, write, _ = await stack.enter_async_context(
                    streamable_http_client("https://mcp.yookassa.ru", http_client=client),
                )
                session = await stack.enter_async_context(ClientSession(read, write))
                await session.initialize()
                yield session

    def run(self, operation, *args):
        if self.pool:
            try:
                return self.pool.run(self.token, operation, *args)
            except McpBusy as error:
                if operation == self.call and args[0] in {"create_payment", "create_invoice"}:
                    raise PaymentNotSent() from error
                raise
        return anyio.run(operation, *args)

    def tools(self) -> set[str]:
        async def available():
            async with self.session() as session:
                listed = await session.list_tools()
                return {tool.name for tool in listed.tools}
        return self.run(available)

    def check(self) -> bool:
        return {"create_payment", "get_payment"} <= self.tools()

    @staticmethod
    def parse(response, schema):
        if response.isError:
            raise ValueError("MCP вернул ошибку операции")
        data = response.structuredContent
        try:
            if data is None and len(response.content) == 1 and response.content[0].type == "text":
                data = json.loads(response.content[0].text)
            return schema.model_validate(data)
        except (ValidationError, ValueError) as error:
            external_id = data.get("id") if isinstance(data, dict) else None
            if isinstance(external_id, str) and 1 <= len(external_id) <= 128:
                raise InvalidPaymentResponse(external_id) from error
            raise InvalidPaymentResponse() from error

    async def call(self, name: str, arguments: dict, schema=Payment):
        tool_started = False
        try:
            async with self.session() as session:
                if name == "create_invoice":
                    listed = await session.list_tools()
                    if not {"create_invoice", "get_invoice"} <= {tool.name for tool in listed.tools}:
                        raise PaymentNotSent()
                tool_started = True
                response = await session.call_tool(name, arguments)
        except Exception as error:
            connection_refused = only_errors(error, (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout))
            if name in {"create_payment", "create_invoice"} and (not tool_started or connection_refused):
                raise PaymentNotSent() from error
            if name in {"get_payment", "get_invoice"} and has_error(error, (ValidationError, ValueError)):
                # SDK валидирует JSON-RPC до возврата результата инструмента.
                raise InvalidPaymentResponse() from error
            raise
        return self.parse(response, schema)

    def create(self, amount: int, attempt_id: str) -> Payment:
        return self.run(
            self.call,
            "create_payment",
            {"amount": provider_amount(amount), "description": payment_reference(attempt_id)},
        )

    def get(self, external_id: str) -> Payment:
        return self.run(self.call, "get_payment", {"id": external_id})

    def create_invoice(self, snapshot: dict, attempt_id: str, order_id: str, created_at: int) -> Invoice:
        amount = provider_amount(snapshot["payable_kopecks"])
        if snapshot["balance_kopecks"]:
            cart = [{"description": f"Предоплата за заказ #{order_id[:8]}", "price": amount, "quantity": 1}]
        else:
            cart = [
                {"description": item["title"][:128], "price": provider_amount(item["unit_kopecks"]), "quantity": item["quantity"]}
                for item in snapshot["items"]
            ]
        return self.run(self.call, "create_invoice", {
            "amount": amount, "description": payment_reference(attempt_id),
            "expires_at": utc_iso(created_at + INVOICE_LIFETIME), "cart": cart,
        }, Invoice)

    def get_invoice(self, external_id: str) -> Invoice:
        return self.run(self.call, "get_invoice", {"id": external_id}, Invoice)

    async def pages(self, name, filters, item_type):
        schema = MCPPage[item_type]
        items = []
        cursor = None
        seen = set()
        async with self.session() as session:
            for _ in range(4):
                arguments = {**filters, "limit": 100}
                if cursor:
                    arguments["next_cursor"] = cursor
                page = self.parse(await session.call_tool(name, arguments), schema)
                items.extend(page.items)
                cursor = page.next_cursor
                if not cursor:
                    return items
                if cursor in seen:
                    break
                seen.add(cursor)
        raise ValueError("ЮKassa вернула неполный список.")

    def find_payment(self, created_at: int, attempt_id: str, amount: int) -> str | None:
        items = self.run(self.pages, "get_payments_list", {
            "created_at_gte": utc_iso(created_at - 120), "created_at_lte": utc_iso(created_at + 600),
        }, ListedPayment)
        candidates = {
            item.id for item in items if item.description == payment_reference(attempt_id)
            and item.amount.currency == "RUB" and item.kopecks == amount
        }
        return next(iter(candidates)) if len(candidates) == 1 else None

    def list_refunds(self, payment_id: str) -> list[Refund]:
        return self.run(self.pages, "get_refunds_list", {"payment_id": payment_id}, Refund)

    def get_refund(self, refund_id: str) -> Refund:
        return self.run(self.call, "get_refund", {"id": refund_id}, Refund)


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
